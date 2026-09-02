"""Месячный потолок расхода на платном тарифе (спринт 183).

ЗАЧЕМ ОТДЕЛЬНО ОТ СУТОЧНОГО. Платный тариф OpenDota считает деньги за
КАЛЕНДАРНЫЙ МЕСЯЦ ($0.01 за 100 вызовов), а суточный лимит про месяц
ничего не знает: 15 000 в сутки — это $46,5 в месяце из 31 дня, и никто
об этом не скажет. Разница между «квота кончилась» и «пришёл счёт» в том,
что первое видно сразу, а второе — в конце месяца и деньгами.

Проверяется на ЖИВОМ Postgres: счётчик общий для всех процессов
коллектора и живёт в базе, а фейковый курсор проверял бы сам себя.

Запуск:
    ./scripts/sql-test.sh tests/test_monthly_budget.py
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector import budget as B  # noqa: E402

DSN = os.environ.get("MANTA_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен MANTA_TEST_DSN на одноразовую базу")

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "migrations" / "postgres"
SCHEMA = "monthly_budget_test"


@pytest.fixture()
def db():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        cur.execute((MIGRATIONS / "011_api_budget.sql").read_text("utf-8"))
        cur.execute(f"SET search_path = {SCHEMA}")
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def spent(db, calls, days_ago=0, source="candidates", api="opendota"):
    day = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO ApiBudget (day, api, source, calls) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (day, api, source) DO UPDATE"
            "   SET calls = ApiBudget.calls + EXCLUDED.calls",
            (day, api, source, calls))


def budget(db, monkeypatch, monthly, limit=1000):
    """ApiBudget поверх ЖИВОГО соединения теста (со схемой-песочницей)."""
    monkeypatch.setattr(B, "MONTHLY_LIMIT", monthly)
    b = B.ApiBudget.__new__(B.ApiBudget)
    b._db = db
    b._source = "candidates"
    b._limit = limit
    b._api = "opendota"
    b._shares = dict(B.SHARES)
    b._global = 100000
    b._monthly = monthly
    b._month_cache = (0.0, 0)
    return b


# -- потолок держит --------------------------------------------------------

def test_the_monthly_ceiling_stops_spending(db, monkeypatch):
    """ГЛАВНОЕ: выбранный месячный потолок останавливает вызовы.

    Без него 15 000 в сутки молча превращаются в счёт на $46 — и
    узнать об этом можно только от провайдера.
    """
    spent(db, 100)
    b = budget(db, monkeypatch, monthly=100)
    with pytest.raises(B.MonthlyBudgetExhausted):
        b.spend(1)


def test_below_the_ceiling_spending_works(db, monkeypatch):
    """Под потолком всё работает как раньше."""
    spent(db, 50)
    b = budget(db, monkeypatch, monthly=100)
    assert b.spend(1) > 0


def test_a_rejected_call_is_not_counted(db, monkeypatch):
    """Отказ не стоит вызова.

    Месячный потолок проверяется ДО инкремента: коллектор в этот момент
    никуда не ходил, и списывать деньги за несостоявшийся запрос нельзя.
    Суточный лимит терпит перелёт (на него оставлен запас), а здесь
    перелёт — это деньги.
    """
    spent(db, 100)
    b = budget(db, monkeypatch, monthly=100)
    with pytest.raises(B.MonthlyBudgetExhausted):
        b.spend(1)
    assert b.month_used(force=True) == 100, "отвергнутый вызов посчитан"


def test_zero_means_no_ceiling(db, monkeypatch):
    """0 — потолка нет: верное умолчание для бесплатного тарифа.

    Там суточный лимит и так не даёт потратить деньги, потому что денег
    нет, и требовать месячный потолок значило бы ломать работающее.
    """
    if datetime.now(timezone.utc).day < 2:
        pytest.skip("первое число: «раньше в этом месяце» ещё не наступало")
    # Расход кладётся ВЧЕРАШНИМ днём: сегодняшний упёрся бы в СУТОЧНЫЙ
    # лимит, и тест проверял бы не то, что заявлено. Поймано на первом
    # прогоне.
    spent(db, 10_000_000, days_ago=1)
    b = budget(db, monkeypatch, monthly=0)
    assert b.spend(1) > 0


# -- что именно считается --------------------------------------------------

def test_all_sources_share_one_ceiling(db, monkeypatch):
    """Потолок общий на API, а не на процесс.

    Платит владелец за сумму. Дай каждому коллектору свой потолок — и
    шестеро выберут шесть потолков.
    """
    spent(db, 60, source="candidates")
    spent(db, 40, source="opendota-timeline")
    b = budget(db, monkeypatch, monthly=100)
    assert b.month_used(force=True) == 100
    with pytest.raises(B.MonthlyBudgetExhausted):
        b.spend(1)


def test_another_api_is_not_counted(db, monkeypatch):
    """Чужой API в наш счёт не идёт.

    Соли GC считаются в той же таблице под своим `api` и денег не стоят
    вовсе; сложи их с OpenDota — и сбор встал бы, не потратив ни цента.
    """
    spent(db, 500, api="steam-gc")
    b = budget(db, monkeypatch, monthly=100)
    assert b.month_used(force=True) == 0
    assert b.spend(1) > 0


def test_last_month_is_not_counted(db, monkeypatch):
    """Прошлый месяц не считается: тариф считает календарный месяц.

    Скользящее окно «за 30 дней» давало бы расход, не совпадающий со
    счётом провайдера, — и спорить пришлось бы своими цифрами против его.
    """
    spent(db, 10_000, days_ago=40)
    b = budget(db, monkeypatch, monthly=100)
    assert b.month_used(force=True) == 0


def test_earlier_days_of_this_month_are_counted(db, monkeypatch):
    """А свои дни этого месяца — считаются.

    Иначе потолок был бы суточным под другим именем.
    """
    today = datetime.now(timezone.utc).date()
    if today.day < 2:
        pytest.skip("первое число: «раньше в этом месяце» ещё не наступало")
    spent(db, 100, days_ago=1)
    b = budget(db, monkeypatch, monthly=150)
    assert b.month_used(force=True) == 100


# -- деньги ----------------------------------------------------------------

def test_the_cost_is_reported_in_dollars(db, monkeypatch):
    """Расход считается в долларах, а не только в вызовах.

    «430 000 вызовов» владельцу не говорит ничего, «$43» — говорит всё.
    """
    spent(db, 10_000)
    b = budget(db, monkeypatch, monthly=100_000)
    assert abs(b.month_cost_usd() - 1.0) < 1e-9


def test_the_refusal_names_the_money_and_the_cure(db, monkeypatch):
    """Отказ называет сумму и то, чем он лечится.

    Сутки его не сбросят — в отличие от суточного, — и без этой строки
    владелец ждал бы утра, а ждать надо новый месяц.
    """
    spent(db, 100)
    b = budget(db, monkeypatch, monthly=100)
    with pytest.raises(B.MonthlyBudgetExhausted) as exc:
        b.spend(1)
    text = str(exc.value)
    assert "МЕСЯЧНЫЙ" in text and "$" in text
    assert "OPENDOTA_MONTHLY_LIMIT" in text


def test_the_monthly_error_is_a_kind_of_budget_error(db, monkeypatch):
    """Месячный отказ ловится там же, где суточный.

    __main__ обрабатывает BudgetExhausted и засыпает вместо стектрейса.
    Отдельный несвязанный тип пролетел бы мимо и уронил цикл.
    """
    assert issubclass(B.MonthlyBudgetExhausted, B.BudgetExhausted)
