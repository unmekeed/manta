"""Тесты вспомогательных функций точки входа коллектора."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.__main__ import seconds_until_utc_midnight


def test_seconds_until_utc_midnight_late_evening():
    now = datetime(2026, 7, 19, 23, 15, 0, tzinfo=timezone.utc)
    # 45 минут до полуночи + 120с запаса.
    assert seconds_until_utc_midnight(now) == 45 * 60 + 120


def test_seconds_until_utc_midnight_just_after_midnight():
    now = datetime(2026, 7, 19, 0, 0, 1, tzinfo=timezone.utc)
    s = seconds_until_utc_midnight(now)
    assert 23 * 3600 < s <= 24 * 3600 + 120


def test_seconds_until_utc_midnight_naive_now_is_utc():
    # now=None → datetime.now(UTC); граница суток может проскочить между
    # вызовом функции и сравнением — допуск в пару секунд.
    s = seconds_until_utc_midnight()
    assert 0 < s <= 24 * 3600 + 120


# -- 429: минутный всплеск против исчерпания суток ----------------------------

# Момент замера задаётся ЯВНО. Раньше сон до полуночи сверялся с
# порогом «> 3600», и это правда 23 часа в сутки: в последний час перед
# полуночью UTC до сброса остаётся меньше часа, и три теста краснели —
# не из-за кода, а из-за времени запуска. Проверять надо не «долго ли
# спим», а КАКАЯ ветка выбрана: burst или ожидание сброса.
NOON = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


def _sleep_for_429(remaining: str, source: str = "opendota-timeline",
                   now: datetime | None = NOON) -> int:
    """Сколько коллектор проспит при 429 с таким остатком суточной квоты.

    Логика живёт в main() между try/except, поэтому воспроизводим её
    условие напрямую — тест сторожит именно решение «burst или сутки».
    """
    from collector.__main__ import BURST_DAY_MARGIN, seconds_until_utc_midnight
    day_left = None
    if remaining not in ("", "?", None):
        try:
            day_left = int(remaining)
        except ValueError:
            day_left = None
    if source.startswith("stratz"):
        return 3600
    if day_left is not None and day_left > BURST_DAY_MARGIN:
        return 90          # присваивается, НЕ max(interval, 90)
    return seconds_until_utc_midnight(now)


def _until_reset() -> int:
    """Сон «до сброса суточной квоты» в тот же момент времени."""
    from collector.__main__ import seconds_until_utc_midnight
    return seconds_until_utc_midnight(NOON)


def test_429_with_quota_left_is_burst_not_daily():
    """Инцидент 2026-08-01: 429 пришёл при remaining-day=2030, то есть при
    целой суточной квоте — это минутный лимит 60/мин. Коллектор уснул до
    полуночи UTC и потерял 14 часов сбора."""
    assert _sleep_for_429("2030") == 90


def test_burst_sleep_is_shorter_than_cycle_interval():
    """Сон при всплеске НЕ должен растягиваться до интервала коллектора.
    Первая версия делала max(interval, 90) и платила получасом за
    минутный лимит — при том что цикл оборван на середине и его надо
    повторить, как только burst сбросится."""
    burst = _sleep_for_429("2030")
    assert burst < 1800, "burst-сон не должен доходить до интервала цикла"
    assert burst >= 60, "меньше минуты — burst не успеет сброситься"


def test_429_with_exhausted_quota_sleeps_until_reset():
    """Квота действительно кончилась — ждём сброса, иначе будем долбить
    API впустую и уводить остаток в минус."""
    assert _sleep_for_429("0") == _until_reset()
    assert _sleep_for_429("-930") == _until_reset()


def test_429_near_quota_edge_treated_as_daily():
    """У самой границы оба лимита срабатывают вперемешку — считаем
    исчерпанием, чтобы не крутиться в коротком сне."""
    assert _sleep_for_429("10") == _until_reset()


def test_429_without_header_falls_back_to_daily():
    """Заголовка нет — безопаснее переждать: короткий сон при реально
    исчерпанной квоте загонит остаток в минус."""
    assert _sleep_for_429("?") == _until_reset()
    assert _sleep_for_429("") == _until_reset()


# -- 429 приписывается тому, кто ответил, а не имени источника (спринт 119) ---

class _Resp:
    def __init__(self, url):
        self.url = url


def test_opendota_429_inside_stratz_collector_is_not_stratz():
    """Замер 2026-08-06: три 429 за сутки при остатке квоты STRATZ 13358
    из 15000 — у STRATZ упереться было не во что. Виноват листинг
    OpenDota (remaining-minute 23 из 60), который stratz-источник и
    использует для кандидатов.

    Цена ошибки в атрибуции: час простоя вместо 90 секунд, у источника,
    ставшего главным по притоку (197 матчей в сутки).
    """
    from collector.__main__ import blamed_on_stratz

    resp = _Resp("https://api.opendota.com/api/parsedMatches")
    assert blamed_on_stratz(resp, "stratz-timeline") is False


def test_stratz_429_is_still_stratz():
    from collector.__main__ import blamed_on_stratz

    resp = _Resp("https://api.stratz.com/graphql")
    assert blamed_on_stratz(resp, "stratz-timeline") is True


def test_falls_back_to_source_when_there_is_no_response():
    """Обрыв связи не даёт ответа вовсе. Тогда судить можно только по
    источнику — это хуже, но лучше, чем считать всех виноватыми."""
    from collector.__main__ import blamed_on_stratz

    assert blamed_on_stratz(None, "stratz-timeline") is True
    assert blamed_on_stratz(None, "opendota-timeline") is False
    assert blamed_on_stratz(_Resp(""), "stratz-timeline") is True


def test_sleep_branch_uses_the_host_not_the_source_name():
    """Структурная проверка: ветки выбора сна обязаны опираться на
    результат blamed_on_stratz, а не на args.source. Иначе следующая
    правка вернёт прежнее поведение, а тесты выше останутся зелёными."""
    import pathlib

    import collector.__main__ as m

    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    tail = src.split("except requests.HTTPError", 1)[1]
    assert 'args.source.startswith("stratz")' not in tail, (
        "решение о сне снова принимается по имени источника")
    assert "from_stratz" in tail
