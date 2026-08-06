"""Тесты суточного бюджета вызовов OpenDota (спринт 130).

Проверяется то, из-за чего спринт и появился: 2026-08-06 один источник
выел общую квоту 2000/сутки и остановил ВСЕ коллекторы на шесть часов,
включая кормящие про-эталон промоушен-гейта.

Ключевое требование — исчерпание бюджета должно БРОСАТЬ, а не возвращать
False: возвращаемое значение слишком легко проигнорировать, и мы уже
дважды на этом обожглись (спринты 126.1 и 129.1, где исчерпанная квота
трактовалась как свойство отдельного матча).
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector import budget  # noqa: E402


class FakeCursor:
    """Курсор поверх словаря — та же семантика, что у UPSERT ... RETURNING."""

    def __init__(self, store):
        self.store = store
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        if sql.startswith("INSERT"):
            day, api, source, n = params
            key = (day, api, source)
            self.store[key] = self.store.get(key, 0) + n
            self._last = (self.store[key],)
        else:
            day, api, source = params
            self._last = (self.store.get((day, api, source), 0),)

    def fetchone(self):
        return self._last


class FakeDB:
    def __init__(self):
        self.store = {}

    def cursor(self):
        return FakeCursor(self.store)

    def close(self):
        pass


def _budget(limit, source="candidates"):
    b = budget.ApiBudget.__new__(budget.ApiBudget)
    b._dsn = "fake"
    b._db = FakeDB()
    b._source = source
    b._limit = limit
    b._api = "opendota"
    return b


def test_spend_counts_and_allows_within_limit():
    b = _budget(3)
    assert [b.spend() for _ in range(3)] == [1, 2, 3]
    assert b.used() == 3


def test_spend_raises_past_the_limit():
    """Бросает, а не возвращает False — иначе вызывающий это проигнорирует."""
    b = _budget(2)
    b.spend()
    b.spend()
    with pytest.raises(budget.BudgetExhausted):
        b.spend()


def test_exhausted_message_names_the_source_and_numbers():
    b = _budget(1, source="opendota-timeline")
    b.spend()
    with pytest.raises(budget.BudgetExhausted, match="opendota-timeline"):
        b.spend()


def test_separate_sources_do_not_share_a_counter():
    """Смысл бюджета: один источник не может выесть долю другого."""
    a, c = _budget(2, "candidates"), _budget(2, "opendota-league")
    a._db = c._db  # общая таблица, как в реальности
    a.spend()
    a.spend()
    with pytest.raises(budget.BudgetExhausted):
        a.spend()
    assert c.spend() == 1, "чужой перерасход задел соседа"


def test_zero_limit_disables_accounting_without_raising():
    """Не настроен — no-op: забытая настройка не должна ронять сбор."""
    b = _budget(0)
    assert b.spend() == 0
    assert b.spend() == 0


def test_module_spend_is_noop_until_configured():
    budget.reset_for_tests()
    budget.spend()          # не должно бросать и не должно ходить в БД
    budget.spend(5)


def test_module_spend_uses_configured_budget():
    budget.reset_for_tests()
    b = _budget(1)
    budget._current = b
    try:
        budget.spend()
        with pytest.raises(budget.BudgetExhausted):
            budget.spend()
    finally:
        budget.reset_for_tests()


def test_day_key_is_utc_not_local(monkeypatch):
    """OpenDota сбрасывает счётчик в 00:00 UTC.

    По московскому календарю бюджет обнулялся бы на три часа раньше — и
    эти три часа мы получали бы 429 на ровном месте.

    Часы подменяются так, чтобы локальная дата и UTC-дата РАЗЛИЧАЛИСЬ:
    иначе тест зелен на машине в UTC (наш контейнер — как раз такая) при
    любом поведении кода и не проверяет ничего.
    """
    from datetime import datetime, timedelta, timezone

    utc_moment = datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # tz=None — «локальное» время, здесь UTC+3: уже 7 августа.
            return utc_moment if tz else (utc_moment.replace(tzinfo=None)
                                          + timedelta(hours=3))

    monkeypatch.setattr(budget, "datetime", FakeDatetime)
    assert _budget(1)._today() == "2026-08-06"
