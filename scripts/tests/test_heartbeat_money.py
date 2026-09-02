"""Расход на платном тарифе виден и вовремя (спринт 183).

ЗАЧЕМ. Потолок, которого никто не видит, защищает от счёта, но не от
неожиданности: упрёшься в него двадцатого числа — сбор встанет до
первого, а узнаешь об этом из тишины. Поэтому расход показывается в
долларах каждый день, а на 80% потолка сторож краснеет.

Порог именно 80%, а не 100%: сообщать о перерасходе, когда он уже
случился, поздно — деньги потрачены.

РАЗДЕЛЕНИЕ ТРУДА. Считает доктор, докладывает сторож. Сторож строится
ПОВЕРХ доктора и своего мнения о состоянии машины не заводит — иначе два
места считали бы одно и то же и однажды посчитали бы по-разному. Первая
версия этого спринта правило нарушила, и поймал её существующий тест
`test_built_on_top_of_doctor_not_beside_it`.
"""
import re
from pathlib import Path

DOCTOR = (Path(__file__).resolve().parents[1] / "doctor.sh").read_text("utf-8")
BEAT = (Path(__file__).resolve().parents[1] / "heartbeat.sh").read_text("utf-8")


def money_block() -> str:
    """Блок расчёта целиком.

    Режется от `if [ "${OPENDOTA_MONTHLY_LIMIT` до конца его `fi`, а не
    по комментариям: заголовочный комментарий блока многострочный, и
    нарезка по «\n# » обрывала его на первой же строке пояснения.
    """
    body = DOCTOR.split('if [ "${OPENDOTA_MONTHLY_LIMIT:-0}" -gt 0 ]', 1)[1]
    return body.split("\nfi\n", 1)[0]


# -- считает доктор -------------------------------------------------------------

def test_the_spend_is_reported_in_dollars():
    """Расход показывается деньгами, а не вызовами.

    «430 000 вызовов» владельцу не говорит ничего, «$43» — говорит всё.
    """
    assert "OPENDOTA_COST_PER_CALL" in DOCTOR
    assert "OpenDota за месяц" in DOCTOR


def test_the_month_is_calendar_not_a_rolling_window():
    """Месяц календарный — тот же, что считает тариф.

    Скользящее окно «за 30 дней» давало бы цифру, не совпадающую со
    счётом провайдера, и спорить пришлось бы своими числами против его.
    """
    block = money_block()
    assert "date_trunc('month'" in block
    assert "AT TIME ZONE 'UTC'" in block


def test_only_opendota_calls_are_counted_as_money():
    """Соли GC денег не стоят и в счёт не идут.

    Они пишутся в ту же таблицу под своим `api`. Сложи их с OpenDota — и
    доктор объявил бы перерасход там, где не потрачено ни цента.
    """
    assert "api = 'opendota'" in money_block()


def test_nothing_is_claimed_without_a_ceiling():
    """Без потолка про доллары молчим.

    На бесплатном тарифе вызовы денег не стоят, и строка про расход была
    бы выдумкой — той самой, из-за которой перестают верить остальным
    строкам отчёта.
    """
    assert 'OPENDOTA_MONTHLY_LIMIT:-0}" -gt 0' in DOCTOR


def test_an_unreachable_database_does_not_fake_a_number():
    """База не ответила — так и сказано, а не «$0.00».

    Ноль при недоступной базе выглядит ровно как по-настоящему
    нетронутый бюджет, то есть врёт в успокаивающую сторону.
    """
    block = money_block()
    assert "2>/dev/null" in block
    assert 'if [ -z "$calls" ]' in block
    assert "не ответила" in block


# -- докладывает сторож ---------------------------------------------------------

def test_the_heartbeat_reads_the_doctor_instead_of_the_database():
    """Сторож берёт цифру из отчёта доктора, а не считает сам.

    Два места, считающие одно и то же, однажды посчитают по-разному.
    """
    assert "doctor_out" in BEAT.split("Деньги за месяц", 1)[1][:400]
    for own in ("psql", "clickhouse-client"):
        assert own not in BEAT, f"сторож сам лезет в базы: {own}"


def test_eighty_percent_turns_the_heartbeat_red():
    """На 80% сторож переходит в «есть проблемы».

    Зелёная галочка рядом со строкой «потрачено 90%» — это не
    предупреждение, а фон: её прочитают так же, как остальные зелёные.
    """
    assert "OPENDOTA_ALERT_PCT:-80" in BEAT
    tail = BEAT.split("OPENDOTA_ALERT_PCT", 1)[1]
    assert "doctor_code=1" in tail
    assert "problems=" in tail


def test_the_alert_says_the_amount():
    """Тревога называет сумму, а не только процент.

    «80% потолка» не отвечает на вопрос, который задают первым: сколько
    это в деньгах.
    """
    # Тревога собирается из строки доктора, а в ней есть сумма.
    assert 'money_alert="• $money"' in BEAT
    assert "$cost" in money_block(), "доктор не печатает сумму"
