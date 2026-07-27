"""Тесты retention-политики отчётов (Гл. 9.7, спринт 72).

Проверяется не SQL (он проверен на живом Postgres), а решения вокруг него:
когда чистка выключена, когда отказывает и почему сухой прогон — дефолт.
Именно здесь цена ошибки несимметрична: лишний день хранения стоит
килобайт, лишнее удаление — часов пересчёта отчётов.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reportgen import retention


class FakeCursor:
    def __init__(self, count):
        self._count = count
        self.executed = []
        self.rowcount = count

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (self._count,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, count):
        self.cur = FakeCursor(count)

    def cursor(self):
        return self.cur


def test_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv(retention.DAYS_ENV, raising=False)
    assert retention.configured_days() == 0


def test_zero_and_negative_mean_disabled(monkeypatch):
    for v in ("0", "-1"):
        monkeypatch.setenv(retention.DAYS_ENV, v)
        assert retention.configured_days() == 0


def test_garbage_disables_instead_of_crashing(monkeypatch):
    """Опечатка в env не должна ронять report-generator: чистка — гигиена,
    а не путь данных."""
    monkeypatch.setenv(retention.DAYS_ENV, "триста")
    assert retention.configured_days() == 0


def test_too_small_period_is_refused(monkeypatch):
    """`REPORTS_RETENTION_DAYS=1` почти наверняка опечатка (хотели 100), а
    последствие — вычищенная база. Отказ громкий, а не тихое выполнение."""
    monkeypatch.setenv(retention.DAYS_ENV, "1")
    with pytest.raises(ValueError):
        retention.configured_days()


def test_valid_period_parsed(monkeypatch):
    monkeypatch.setenv(retention.DAYS_ENV, "180")
    assert retention.configured_days() == 180


def test_dry_run_is_default_and_deletes_nothing():
    conn = FakeConn(count=42)
    assert retention.purge(conn, 180) == 42
    sqls = [sql for sql, _ in conn.cur.executed]
    assert all("DELETE" not in s for s in sqls), \
        "без apply=True удаления быть не должно"
    assert any("count(*)" in s for s in sqls)


def test_apply_issues_delete():
    conn = FakeConn(count=42)
    assert retention.purge(conn, 180, apply=True) == 42
    assert any("DELETE" in sql for sql, _ in conn.cur.executed)


def test_nothing_to_delete_skips_delete():
    """Пустой прогон не должен слать DELETE: лишняя мутация в логах БД
    маскирует настоящие чистки."""
    conn = FakeConn(count=0)
    assert retention.purge(conn, 180, apply=True) == 0
    assert all("DELETE" not in sql for sql, _ in conn.cur.executed)


def test_zero_days_is_noop():
    conn = FakeConn(count=99)
    assert retention.purge(conn, 0, apply=True) == 0
    assert conn.cur.executed == [], "при выключенном retention запросов нет"
