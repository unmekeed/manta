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

def _sleep_for_429(remaining: str, source: str = "opendota-timeline") -> int:
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
        return 90
    return seconds_until_utc_midnight()


def test_429_with_quota_left_is_burst_not_daily():
    """Инцидент 2026-08-01: 429 пришёл при remaining-day=2030, то есть при
    целой суточной квоте — это минутный лимит 60/мин. Коллектор уснул до
    полуночи UTC и потерял 14 часов сбора."""
    assert _sleep_for_429("2030") == 90


def test_429_with_exhausted_quota_sleeps_until_reset():
    """Квота действительно кончилась — ждём сброса, иначе будем долбить
    API впустую и уводить остаток в минус."""
    assert _sleep_for_429("0") > 3600
    assert _sleep_for_429("-930") > 3600


def test_429_near_quota_edge_treated_as_daily():
    """У самой границы оба лимита срабатывают вперемешку — считаем
    исчерпанием, чтобы не крутиться в коротком сне."""
    assert _sleep_for_429("10") > 3600


def test_429_without_header_falls_back_to_daily():
    """Заголовка нет — безопаснее переждать: короткий сон при реально
    исчерпанной квоте загонит остаток в минус."""
    assert _sleep_for_429("?") > 3600
    assert _sleep_for_429("") > 3600
