"""Обрыв у внешнего API не стоит целого интервала (спринт 146).

ЧТО СЛУЧИЛОСЬ. 2026-08-19 api.opendota.com отдавал 522 — Cloudflare не
достучался до origin. Проверено с двух разных сетей: лежала не наша
машина. Такие обрывы длятся минуты.

Коллектор при этом ложился спать на ПОЛНЫЙ интервал цикла. У
opendota-public это час:

    ERROR цикл сбора упал; повтор через 3600s
    requests.exceptions.HTTPError: 522 Server Error

То есть двадцатисекундная неполадка у чужого сервера стоила часа сбора, и
так каждый раз. Ветка `else` не различала «нас ограничили по квоте» и «у
них упал сервер» — а это ровно противоположные случаи: в первом ждать
надо дольше обычного, во втором короче.

ПОЧЕМУ НЕ ПОВТОР ЗАПРОСА В ЦИКЛЕ. budget.spend() стоит ПЕРЕД запросом, и
у opendota-public бюджет 50 вызовов в сутки. Повтор внутри цикла жёг бы
квоту тем быстрее, чем дольше лежит чужой сервер. Здесь цена обрыва — не
лишние вызовы, а более ранний следующий цикл.
"""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.__main__ import TransientBackoff, is_transient  # noqa: E402


class _Resp:
    def __init__(self, code):
        self.status_code = code


def _http(code):
    return requests.HTTPError(f"{code} Server Error", response=_Resp(code))


# -- что считать обрывом ------------------------------------------------------

@pytest.mark.parametrize("code", [500, 502, 503, 504, 520, 522, 524])
def test_server_side_codes_are_transient(code):
    """5xx — сбой у них. 522 в этом списке не случайно: именно он и был."""
    assert is_transient(_http(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429])
def test_client_side_codes_are_not_transient(code):
    """429 сюда попасть не должен НИ В КОЕМ СЛУЧАЕ.

    Это не обрыв, а исчерпанная квота: у неё своя пауза, куда длиннее.
    Спутать значило бы долбить API каждые пять минут при отрицательном
    остатке — ровно то, от чего защищались в спринтах 119 и 134.
    """
    assert is_transient(_http(code)) is False


def test_connection_errors_are_transient():
    """Обрыв связи до HTTPError не доходит вовсе.

    requests бросает ConnectionError или Timeout, и такой сбой падал в
    ОБЩУЮ ветку `except Exception` — то есть стоил полного интервала даже
    после починки ветки HTTP-ошибок.
    """
    assert is_transient(requests.ConnectionError("сеть недоступна")) is True
    assert is_transient(requests.Timeout("истекло")) is True


def test_unknown_failures_are_not_transient():
    """Наша собственная ошибка — не повод торопиться с повтором.

    Опечатка в коде повторится и через пять минут, и через час; частить с
    ней значит залить лог одинаковыми трассировками.
    """
    assert is_transient(ValueError("баг в разборе")) is False
    assert is_transient(requests.HTTPError("без ответа", response=None)) is False


# -- сама пауза ---------------------------------------------------------------

def test_first_failure_retries_much_sooner_than_the_interval():
    """Главное утверждение спринта."""
    b = TransientBackoff(base=300)
    assert b.next_sleep(3600) == 300


def test_pause_doubles_with_every_failure_in_a_row():
    """Растущая, а не постоянная: при долгом падении API постоянные 300с
    дали бы двенадцать бесполезных циклов в час."""
    b = TransientBackoff(base=300)
    assert [b.next_sleep(3600) for _ in range(4)] == [300, 600, 1200, 2400]


def test_pause_never_exceeds_the_normal_interval():
    """Механизм УКОРАЧИВАЕТ паузу, а не удлиняет её.

    Иначе долгий сбой наказывал бы сильнее, чем прежнее поведение, — и
    починка вышла бы дороже поломки.
    """
    b = TransientBackoff(base=300)
    assert [b.next_sleep(3600) for _ in range(8)][-3:] == [3600, 3600, 3600]


def test_short_interval_collectors_are_unaffected():
    """У кого цикл и так короче паузы — для того ничего не меняется.

    timeline-коллекторы ходят чаще; им нечего укорачивать, и трогать их
    поведение эта правка не должна.
    """
    b = TransientBackoff(base=300)
    assert [b.next_sleep(120) for _ in range(3)] == [120, 120, 120]


def test_reset_returns_to_the_first_step():
    """Успешный цикл обнуляет счётчик.

    Без этого пауза росла бы через весь день: пять разрозненных обрывов
    за сутки навсегда перевели бы коллектор на интервал вместо пяти
    минут, и следующий короткий сбой снова стоил бы часа.
    """
    b = TransientBackoff(base=300)
    b.next_sleep(3600)
    b.next_sleep(3600)
    b.reset()
    assert b.failures == 0
    assert b.next_sleep(3600) == 300


# -- проверки того, что механизм ВЫЗЫВАЮТ -------------------------------------
#
# Проверки ниже читают исходник главного цикла. Это слабее поведенческих,
# и берутся они не от хорошей жизни: цикл живёт внутри main() между
# разбором аргументов и созданием коллектора, поведенчески его отсюда не
# достать. Но привычная дыра — безупречная функция, которую никто не
# зовёт, — стоила в этом развёртывании двух спринтов подряд (профили
# коллекторов, verify_running), и оставлять её открытой дороже.

def _main_source() -> str:
    import collector.__main__ as m
    return Path(m.__file__).read_text(encoding="utf-8")


def _handler(start: str, end: str) -> str:
    """Тело одного обработчика из главного цикла."""
    src = _main_source()
    assert start in src and end in src, "разметка обработчиков съехала"
    return src.split(start, 1)[1].split(end, 1)[0]


def test_http_errors_use_the_transient_pause():
    """Ветка HTTP-ошибок различает обрыв и отказ по квоте."""
    body = _handler("except requests.HTTPError as e:",
                    "except budget.BudgetExhausted")
    assert "is_transient(e)" in body and "backoff.next_sleep(" in body, (
        "5xx снова стоит полного интервала")


def test_connection_errors_use_the_transient_pause():
    """И ОБЩАЯ ветка тоже — а это отдельный путь.

    Обрыв связи до HTTPError не доходит: requests бросает
    ConnectionError или Timeout, и такой сбой падает сюда. Починить одну
    ветку HTTP-ошибок и остановиться значило бы оставить нетронутым ровно
    тот случай, когда чужой сервер не отвечает совсем, — а это и есть
    самый частый вид обрыва. Мутация, снимавшая эту ветку, пережила
    первый заход тестов: is_transient был проверен, его ВЫЗОВ — нет.
    """
    # Конец обработчика — выход из тела цикла: `if args.once:` с ОТСТУПОМ
    # ЦИКЛА, а не обработчика. Без отступа маркер совпал бы с первым же
    # `if args.once: raise` внутри самого обработчика, и срез оборвался бы
    # на первой строке — тест падал бы на верном коде.
    body = _handler("except Exception as e:", "\n            if args.once:")
    assert "is_transient(e)" in body and "backoff.next_sleep(" in body, (
        "обрыв связи снова стоит полного интервала")


def test_success_resets_the_backoff():
    """reset() зовётся В ТЕЛЕ успешного цикла, а не где-нибудь ещё."""
    body = _main_source().split("n = collector.collect_once()", 1)[1]
    body = body.split("except", 1)[0]
    assert "backoff.reset()" in body, (
        "успешный цикл не обнуляет счётчик обрывов — пауза будет расти вечно")


def test_quota_branch_does_not_use_the_transient_pause():
    """Ветка 429 обязана остаться со своей длинной паузой.

    Если короткая пауза протечёт в неё, коллектор начнёт долбить API при
    исчерпанной квоте — поломка дороже той, что чинится.
    """
    src = _main_source()
    quota = src.split("elif status == 429:", 1)[1].split("elif is_transient", 1)[0]
    assert "next_sleep" not in quota
    assert "seconds_until_utc_midnight()" in quota, "разметка веток съехала"
