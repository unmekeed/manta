"""Дедуп по ВОЗМОЖНОСТЯМ, а не по факту сбора (спринт 151).

ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ С НАСТОЯЩИМ POSTGRES. Смысл целиком живёт в SQL:
в предикате `AND has_replay` и в том, какой из двух путей делает
`DO UPDATE`, а какой `DO NOTHING`. Фейковый курсор возвращает то, что в
него положили, и любая мутация предиката проходит мимо — а здесь как раз
предикат и есть вся правка.

ЧТО СЛУЧИЛОСЬ. На VPS витрина росла (106 матчей), а PositionSnapshots
оставался пуст: ни одной тепловой карты. CollectedMatches одна на все
источники, и это записано в докстроке timeline_runner как достоинство:
«один матч никогда не въезжает дважды, каким бы путём ни пришёл».

Достоинство обернулось тупиком. JSON-путь быстрее и жаднее реплейного
(14 матчей за 30 минут против 2 за час) и разбирает те же про-матчи.
Каждый такой матч попадал в CollectedMatches, после чего реплейный
источник видел его дубликатом и пропускал навсегда:

    skip duplicate match_id=8943098449
    skip duplicate match_id=8943142948
    cycle done, processed=0

А у JSON-матчей координат нет в принципе. Карты не появились бы никогда —
и не из-за поломки, а по устройству.

Запуск:
    make dedup-sql-test
или вручную:
    MANTA_TEST_DSN=postgresql://... pytest tests/test_dedup_sql.py

Без MANTA_TEST_DSN файл пропускается: в CI постоянного Postgres нет, и
это честно означает «здесь эта проверка не выполнялась», а не «зелено».

Рабочую базу тест не трогает: он живёт в собственной схеме, создаёт и
удаляет только её.
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

SCHEMA = "dedup_sql_test"


@pytest.fixture()
def db():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        # Схема из НАСТОЯЩИХ миграций: своя копия CREATE TABLE проверяла бы
        # саму себя, а не то, что приезжает на прод. 002 заводит таблицу,
        # 012 добавляет has_replay и заполняет его для старых строк.
        for name in ("002_outbox.sql", "012_collected_has_replay.sql"):
            cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        cur.execute(f"SET search_path = {SCHEMA}")
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _replay_runner(conn):
    from collector.runner import Collector

    c = Collector.__new__(Collector)
    c._db = conn
    c._source = type("S", (), {"name": "opendota"})()
    return c


def _timeline_runner(conn):
    from collector.timeline_runner import TimelineCollector

    c = TimelineCollector.__new__(TimelineCollector)
    c._db = conn
    c._source = type("S", (), {"name": "opendota-timeline-pro"})()
    c._cfg = None
    return c


def _row(conn, match_id):
    with conn.cursor() as cur:
        cur.execute("SELECT source_name, replay_url, has_replay"
                    " FROM CollectedMatches WHERE match_id = %s", (match_id,))
        return cur.fetchone()


def _ref(match_id):
    from collector.sources import MatchRef
    return MatchRef(match_id=match_id, replay_url=f"http://x/{match_id}.bz2",
                    tier="Professional", source_cursor=str(match_id))


# -- главное утверждение спринта ---------------------------------------------

def test_json_collected_match_is_not_a_duplicate_for_the_replay_path(db):
    """Матч, взятый JSON-путём, реплейный обязан ДОСТРОИТЬ.

    Ровно та строка, из-за которой карт не было: у JSON-матча нет ни
    позиций, ни событий, и отказ от него — отказ от единственного
    источника координат.
    """
    _timeline_runner(db)._mark_collected(111, "111")
    assert _replay_runner(db)._is_collected(111) is False


def test_replay_collected_match_is_a_duplicate_for_the_replay_path(db):
    """А вот повторно качать реплей незачем: 100 МиБ и квота."""
    _replay_runner(db)._mark_collected(_ref(222), "http://x/222.bz2")
    assert _replay_runner(db)._is_collected(222) is True


def test_unknown_match_is_not_a_duplicate(db):
    assert _replay_runner(db)._is_collected(999) is False


# -- отметка после сбора ------------------------------------------------------

def test_replay_path_upgrades_an_existing_json_row(db):
    """DO UPDATE, а не DO NOTHING.

    Оставь мы DO NOTHING — has_replay навсегда остался бы FALSE, и
    реплейный путь качал бы один и тот же матч каждый цикл. Дедуп,
    починенный наполовину, хуже сломанного: он жжёт квоту.
    """
    _timeline_runner(db)._mark_collected(333, "333")
    assert _row(db, 333)[2] is False

    _replay_runner(db)._mark_collected(_ref(333), "http://x/333.bz2")
    src, url, has = _row(db, 333)
    assert has is True
    assert url == "http://x/333.bz2" and src == "opendota"
    # И теперь он действительно перестал быть кандидатом.
    assert _replay_runner(db)._is_collected(333) is True


def test_json_path_never_downgrades_a_replay_row(db):
    """Обратное направление обязано молчать.

    Понизь JSON-путь чужую отметку — и реплейный источник отправился бы
    качать заново то, что уже разобрано.
    """
    _replay_runner(db)._mark_collected(_ref(444), "http://x/444.bz2")
    _timeline_runner(db)._mark_collected(444, "444")
    src, url, has = _row(db, 444)
    assert has is True
    assert src == "opendota" and url == "http://x/444.bz2"


def test_json_path_still_skips_everything_already_collected(db):
    """JSON-путь пропускает и своё, и чужое.

    Реплей даёт всё то же, что JSON, и сверх того, поэтому идти по нему
    второй раз незачем. Ослабь мы и эту сторону — один матч писался бы в
    витрину дважды разными путями.
    """
    tl = _timeline_runner(db)
    _replay_runner(db)._mark_collected(_ref(555), "http://x/555.bz2")
    assert tl._is_collected(555) is True

    tl._mark_collected(666, "666")
    assert tl._is_collected(666) is True


# -- миграция ------------------------------------------------------------------

def test_migration_backfills_by_the_url_prefix(db):
    """Уже собранные строки получают has_replay по replay_url.

    Другого свидетельства нет: JSON-путь с самого начала писал туда
    «json:<источник>», реплейный — настоящий адрес. Ошибись backfill — и
    все прежние реплейные матчи скачались бы заново, а это сотни мегабайт
    и вся суточная квота.
    """
    with db.cursor() as cur:
        # Строки в том виде, в каком они лежали ДО миграции: колонки нет,
        # поэтому вставляем без неё и заново прогоняем миграцию.
        cur.execute("ALTER TABLE CollectedMatches DROP COLUMN has_replay")
        cur.execute(
            "INSERT INTO CollectedMatches (match_id, source_name, replay_url)"
            " VALUES (1, 'opendota', 'http://replay1.valve.net/1.bz2'),"
            "        (2, 'opendota-timeline-pro', 'json:opendota-timeline-pro')")
        cur.execute((MIGRATIONS / "012_collected_has_replay.sql")
                    .read_text(encoding="utf-8"))
        cur.execute(f"SET search_path = {SCHEMA}")

    assert _row(db, 1)[2] is True, "реплейный матч объявлен несобранным"
    assert _row(db, 2)[2] is False, "JSON-матч объявлен имеющим реплей"


def test_migration_is_idempotent(db):
    """Повторный прогон ничего не портит: bootstrap гоняют многократно."""
    sql = (MIGRATIONS / "012_collected_has_replay.sql").read_text(
        encoding="utf-8")
    _timeline_runner(db)._mark_collected(777, "777")
    with db.cursor() as cur:
        cur.execute(sql)
        cur.execute(f"SET search_path = {SCHEMA}")
    assert _row(db, 777)[2] is False
