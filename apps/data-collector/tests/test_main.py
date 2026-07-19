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
