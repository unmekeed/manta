"""Парковка на ЖИВОМ Postgres (спринт 153).

Смысл ParkedStore целиком в запросах: «всё ещё нужен» — это LEFT JOIN с
CollectedMatches по has_replay, «повторная парковка» — ON CONFLICT DO
UPDATE со счётчиком. Фейковый курсор такое не проверяет: он возвращает
то, что в него положили, и любая мутация предиката проходит мимо.

Ключевое свойство, которое здесь и сторожится: припаркованный матч
исчезает из выдачи САМ, когда реплей появляется. Удаляй мы строку в
обработчике успеха, у факта «матч собран» стало бы две точки правды — а
чем это кончается, спринт 151 показал наглядно: реплейный путь считал
JSON-матчи дубликатами и не построил ни одной карты.

Запуск:
    ./scripts/sql-test.sh tests/test_parking_sql.py

Без MANTA_TEST_DSN файл пропускается: в CI постоянного Postgres нет, и
это честно означает «здесь не выполнялось», а не «зелено».
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MANTA_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен MANTA_TEST_DSN на одноразовую базу")

MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / \
    "infra" / "migrations" / "postgres"

SCHEMA = "parking_sql_test"

CN = "http://replay413.dota2.com.cn/570/{}_x.dem.bz2"
EU = "http://replay181.valve.net/570/{}_x.dem.bz2"


@pytest.fixture()
def store():
    from collector.parked import ParkedStore

    db = psycopg.connect(DSN, autocommit=True)
    with db.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        # Схема из НАСТОЯЩИХ миграций: своя копия проверяла бы сама себя.
        for name in ("002_outbox.sql", "012_collected_has_replay.sql",
                     "013_parked_replays.sql"):
            cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        cur.execute(f"SET search_path = {SCHEMA}")
    yield ParkedStore(db)
    with db.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    db.close()


def _collected(store, match_id, has_replay):
    with store._db.cursor() as cur:
        cur.execute(
            "INSERT INTO CollectedMatches"
            " (match_id, source_name, replay_url, has_replay)"
            " VALUES (%s, 'x', 'u', %s)", (match_id, has_replay))


def _row(store, match_id):
    with store._db.cursor() as cur:
        cur.execute("SELECT host, reason, attempts FROM ParkedReplays"
                    " WHERE match_id = %s", (match_id,))
        return cur.fetchone()


def test_parked_match_is_wanted(store):
    store.park(1, CN.format(1), "хост недостижим")
    assert store.wanted(10) == [(1, CN.format(1))]


def test_host_is_stored_separately(store):
    """Недостижимость — свойство ХОСТА, и по нему видно, вернулся ли он."""
    store.park(1, CN.format(1), "таймаут")
    host, reason, attempts = _row(store, 1)
    assert host == "replay413.dota2.com.cn"
    assert reason == "таймаут" and attempts == 1


def test_reparking_counts_attempts_and_keeps_the_row(store):
    """Повторная парковка не плодит строки и не теряет историю."""
    store.park(1, CN.format(1), "первый раз")
    store.park(1, CN.format(1), "второй раз")
    host, reason, attempts = _row(store, 1)
    assert attempts == 2 and reason == "второй раз"
    assert len(store.wanted(10)) == 1


def test_a_json_collected_match_is_still_wanted(store):
    """Матч, взятый JSON-путём, из парковки НЕ исчезает.

    У него нет ни позиций, ни событий — то есть желание не исполнено.
    Спутай мы это с «собран», парковка потеряла бы ровно те матчи, ради
    которых заведена.
    """
    store.park(1, CN.format(1), "таймаут")
    _collected(store, 1, has_replay=False)
    assert store.wanted(10) == [(1, CN.format(1))]


def test_a_replayed_match_leaves_the_list_by_itself(store):
    """ГЛАВНОЕ: желание исполнено — строка перестаёт отдаваться.

    Без удаления и без обработчика успеха. Вторая точка правды о том, что
    матч собран, здесь не заводится: первая уже есть, и это
    CollectedMatches.has_replay.
    """
    store.park(1, CN.format(1), "таймаут")
    _collected(store, 1, has_replay=True)
    assert store.wanted(10) == []


def test_wanted_is_ordered_oldest_first_and_limited(store):
    """Старые вперёд: реплеи Valve со временем снимает с раздачи."""
    for mid in (30, 10, 20):
        store.park(mid, CN.format(mid), "таймаут")
    assert [m for m, _ in store.wanted(2)] == [10, 20]


def test_prune_removes_only_the_granted(store):
    """Уборка не трогает то, что ещё ждёт."""
    store.park(1, CN.format(1), "таймаут")
    store.park(2, CN.format(2), "таймаут")
    _collected(store, 1, has_replay=True)
    assert store.prune() == 1
    assert [m for m, _ in store.wanted(10)] == [2]


def test_prune_is_idempotent(store):
    store.park(1, CN.format(1), "таймаут")
    _collected(store, 1, has_replay=True)
    store.prune()
    assert store.prune() == 0


def test_stats_group_by_host(store):
    """По чему именно стоит очередь — видно одним запросом."""
    store.park(1, CN.format(1), "таймаут")
    store.park(2, CN.format(2), "таймаут")
    store.park(3, EU.format(3), "503")
    assert store.stats() == [("replay413.dota2.com.cn", 2),
                             ("replay181.valve.net", 1)]


def test_stats_ignore_the_granted(store):
    """Собранный матч не должен числиться ждущим."""
    store.park(1, CN.format(1), "таймаут")
    store.park(2, CN.format(2), "таймаут")
    _collected(store, 1, has_replay=True)
    assert store.stats() == [("replay413.dota2.com.cn", 1)]


def test_long_reason_does_not_break_the_insert(store):
    """Текст ошибки бывает длинным — обрезаем, а не падаем.

    Сообщение ConnectTimeout содержит и адрес, и адрес объекта в памяти;
    падение на длине означало бы, что матч теряется ровно тогда, когда
    ошибка самая подробная.
    """
    store.park(1, CN.format(1), "x" * 5000)
    assert len(_row(store, 1)[1]) <= 200
