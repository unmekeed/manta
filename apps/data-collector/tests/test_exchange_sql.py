"""Слияние слепков на ЖИВОМ Postgres (спринт 156).

ЗАЧЕМ ЗДЕСЬ. Обмен между машинами вливает архив в базу, где УЖЕ что-то
есть, и весь смысл переноса живёт в хвосте ON CONFLICT. Проверять его
текстом бесполезно: строка «GREATEST(playerranks.seen_count,
EXCLUDED.seen_count)» выглядит одинаково верной и когда она верна, и
когда должна была быть суммой. Нужен настоящий Postgres.

Файл лежит среди тестов коллектора, а не рядом со скриптом, по той же
причине, что и остальные *_sql.py: scripts/sql-test.sh гоняет их в образе
коллектора — единственном, где уже есть psycopg. Сам скрипт монтирует
репозиторий целиком, поэтому bash-функции обмена читаются отсюда прямо из
scripts/lib/dataset-tables.sh, а не переписываются копией. Копия
проверяла бы сама себя.

ГЛАВНОЕ, ЧТО СТОРОЖИТСЯ. Кэш рангов стоит квоты OpenDota — 2000 запросов
в сутки, и купить больше нельзя. Строка «этот аккаунт встречен в потоке»
не стоит ничего. Слейся они неверно — бесплатная встреча затёрла бы
оплаченный ранг, и заметить это было бы нечем: в таблице просто снова
появился бы NULL, неотличимый от «ещё не спрашивали».

Запуск:
    ./scripts/sql-test.sh tests/test_exchange_sql.py
"""
import os
import pathlib
import subprocess

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MANTA_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен MANTA_TEST_DSN на одноразовую базу")

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "migrations" / "postgres"
TABLES_SH = ROOT / "scripts" / "lib" / "dataset-tables.sh"

SCHEMA = "exchange_sql_test"


def merge_sql(table: str, cols: str) -> str:
    """Хвост ON CONFLICT — из НАСТОЯЩЕГО скрипта обмена."""
    r = subprocess.run(
        ["bash", "-c", f'. "{TABLES_SH}"; pg_merge_sql "$1" "$2"',
         "_", table, cols],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture()
def db():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        # Схема из НАСТОЯЩИХ миграций: своя копия проверяла бы сама себя.
        for name in ("002_outbox.sql", "003_reports.sql",
                     "006_player_ranks.sql", "012_collected_has_replay.sql"):
            cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        cur.execute(f"SET search_path = {SCHEMA}")
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def merge(db, table, cols, rows):
    """Влить строки так, как это делает импорт: те же колонки, тот же хвост."""
    names = ", ".join(cols)
    holes = ", ".join(["%s"] * len(cols))
    sql = (f"INSERT INTO {table} ({names}) VALUES ({holes}) "
           f"{merge_sql(table, ','.join(cols))}")
    with db.cursor() as cur:
        for row in rows:
            cur.execute(sql, row)


def one(db, sql, args=()):
    with db.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


RANK_COLS = ("account_id", "rank_tier", "source", "seen_count",
             "seen_at", "checked_at")

OLD = "2026-08-01T00:00:00+00:00"
NEW = "2026-08-15T00:00:00+00:00"


# -- кэш рангов ---------------------------------------------------------------

def test_unasked_row_does_not_erase_a_known_rank(db):
    """ГЛАВНОЕ утверждение спринта.

    На машине A аккаунт спрошен и оказался иммортал (85). На машине B тот
    же аккаунт всего лишь встречен в потоке — ранга нет, checked_at NULL.
    Слепок B, влитый в A, не смеет стереть 85.

    Простое «побеждает пришедший» сделало бы именно это, и вернуть ранг
    можно было бы только повторным запросом — то есть снова квотой. А
    выглядела бы потеря как «аккаунт ещё не опрашивали».
    """
    merge(db, "playerranks", RANK_COLS, [(7, 85, "opendota", 3, OLD, OLD)])
    merge(db, "playerranks", RANK_COLS, [(7, None, None, 1, NEW, None)])
    assert one(db, "SELECT rank_tier, source FROM playerranks"
                   " WHERE account_id = 7") == (85, "opendota")


def test_fresher_answer_wins(db):
    """Ранг меняется неделями: кто спрашивал позже, тот и прав."""
    merge(db, "playerranks", RANK_COLS, [(8, 71, "stratz", 1, OLD, OLD)])
    merge(db, "playerranks", RANK_COLS, [(8, 80, "opendota", 1, NEW, NEW)])
    assert one(db, "SELECT rank_tier, source FROM playerranks"
                   " WHERE account_id = 8") == (80, "opendota")


def test_staler_answer_loses(db):
    """И обратно — иначе порядок слияния слепков решал бы за нас.

    Слепки втягиваются в произвольном порядке (peer-sync берёт что нашёл),
    так что «побеждает последний пришедший» означало бы «ранг зависит от
    имени файла».
    """
    merge(db, "playerranks", RANK_COLS, [(9, 80, "opendota", 1, NEW, NEW)])
    merge(db, "playerranks", RANK_COLS, [(9, 71, "stratz", 1, OLD, OLD)])
    assert one(db, "SELECT rank_tier FROM playerranks"
                   " WHERE account_id = 9") == (80,)


def test_seen_count_is_maxed_not_summed(db):
    """Счётчик встреч — приоритет опроса, а не статистика.

    Обе машины идут по ОДНОМУ потоку Valve и видят одни и те же матчи.
    Сумма надула бы приоритет вдвое на ровном месте, и очередь на опрос
    (а она и есть распределение квоты) начала бы отражать число машин, а
    не частоту игрока.
    """
    merge(db, "playerranks", RANK_COLS, [(10, None, None, 40, OLD, None)])
    merge(db, "playerranks", RANK_COLS, [(10, None, None, 25, NEW, None)])
    assert one(db, "SELECT seen_count FROM playerranks"
                   " WHERE account_id = 10") == (40,)


def test_seen_count_grows_to_the_larger_value(db):
    """Обратная сторона: MAX обязан и повышать, а не только защищать.

    Мутация «взять всегда своё» прошла бы предыдущий тест целиком.
    """
    merge(db, "playerranks", RANK_COLS, [(11, None, None, 5, OLD, None)])
    merge(db, "playerranks", RANK_COLS, [(11, None, None, 60, NEW, None)])
    assert one(db, "SELECT seen_count FROM playerranks"
                   " WHERE account_id = 11") == (60,)


def test_checked_at_keeps_the_later_of_the_two(db):
    """NULL в GREATEST не побеждает — на этом всё правило и держится.

    Веди GREATEST себя как в других СУБД (NULL заражает результат),
    встреча в потоке обнуляла бы checked_at, и аккаунт возвращался бы в
    очередь на опрос вечно — тот самый механизм, что задушил STRATZ в
    спринте 87.
    """
    merge(db, "playerranks", RANK_COLS, [(12, 80, "opendota", 1, OLD, OLD)])
    merge(db, "playerranks", RANK_COLS, [(12, None, None, 1, NEW, None)])
    got, = one(db, "SELECT checked_at IS NOT NULL FROM playerranks"
                   " WHERE account_id = 12")
    assert got is True


def test_a_rank_without_checked_at_is_still_protected(db):
    """Правило не опирается на то, каких строк мы «не делаем».

    Сегодня rank_tier без checked_at коллектор не пишет: и то, и другое
    ставится одним UPDATE после ответа. Убери из условия проверку
    «спрашивающая сторона вообще спрашивала» — и на такой строке ранг
    стёрся бы встречей в потоке.

    Проверка узкая нарочно: на всех остальных формах строки условие
    избыточно (Postgres и так даёт NULL на сравнении с NULL, а NULL в
    CASE WHEN ведёт себя как ложь), и без этого теста его можно было бы
    «упростить», ничего не сломав ВИДИМО.
    """
    with db.cursor() as cur:
        cur.execute("INSERT INTO playerranks (account_id, rank_tier, source,"
                    " seen_count, checked_at) VALUES (14, 85, 'opendota', 1, NULL)")
    merge(db, "playerranks", RANK_COLS, [(14, None, None, 2, NEW, None)])
    assert one(db, "SELECT rank_tier FROM playerranks"
                   " WHERE account_id = 14") == (85,)


def test_new_account_is_simply_inserted(db):
    """Слияние не должно мешать обычной вставке."""
    merge(db, "playerranks", RANK_COLS, [(13, 80, "opendota", 2, NEW, NEW)])
    assert one(db, "SELECT rank_tier, seen_count FROM playerranks"
                   " WHERE account_id = 13") == (80, 2)


def test_rank_merge_covers_every_column_of_the_table(db):
    """Правило написано под КОЛОНКИ АРХИВА, а не под свой список.

    Добавь миграция колонку в PlayerRanks — рукописный перечень тихо
    перестал бы её сливать, и увидеть это можно было бы только сравнив
    две базы построчно. Здесь колонки берутся из схемы и сверяются с тем,
    что попало в SET.
    """
    with db.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = 'playerranks'"
                    " ORDER BY ordinal_position", (SCHEMA,))
        cols = [r[0] for r in cur.fetchall()]
    sql = merge_sql("playerranks", ",".join(cols))
    missing = [c for c in cols
               if c != "account_id" and f"{c} =" not in sql]
    assert not missing, f"колонки не участвуют в слиянии: {missing}"


# -- признак реплея -----------------------------------------------------------

CM_COLS = ("match_id", "source_name", "replay_url", "collected_at",
           "has_replay")


def test_has_replay_is_upgraded_never_downgraded(db):
    """Тот же закон, что у коллектора: повышаем, но не понижаем.

    Машина A взяла матч по таймлайну (реплея нет), машина B разобрала
    реплей. Сам файл в слепок не едет, но ВСЁ, что из него посчитано —
    события, драки, карты, — едет. Оставь тут DO NOTHING, и A полезла бы
    качать 58 МиБ ради данных, которые только что втянула.
    """
    merge(db, "collectedmatches", CM_COLS, [(1, "timeline", "json:1", OLD, False)])
    merge(db, "collectedmatches", CM_COLS, [(1, "opendota", "http://x", NEW, True)])
    assert one(db, "SELECT has_replay FROM collectedmatches"
                   " WHERE match_id = 1") == (True,)


def test_has_replay_is_not_lost_to_a_timeline_snapshot(db):
    """И обратно: чужой слепок без реплея не гасит наш признак.

    Иначе обмен между двумя машинами превращался бы в качели, и каждая
    ночь возвращала бы уже собранные матчи в очередь на скачивание.
    """
    merge(db, "collectedmatches", CM_COLS, [(2, "opendota", "http://x", OLD, True)])
    merge(db, "collectedmatches", CM_COLS, [(2, "timeline", "json:2", NEW, False)])
    assert one(db, "SELECT has_replay FROM collectedmatches"
                   " WHERE match_id = 2") == (True,)


# -- отчёты -------------------------------------------------------------------

MR_COLS = ("match_id", "analysis", "timeline", "model_version",
           "feature_version", "generated_at")


def test_report_merge_keeps_the_fresher_one(db):
    merge(db, "matchreports", MR_COLS,
          [(3, "{}", "{}", "v1", "f1", OLD)])
    merge(db, "matchreports", MR_COLS,
          [(3, '{"a": 1}', "{}", "v2", "f2", NEW)])
    assert one(db, "SELECT model_version FROM matchreports"
                   " WHERE match_id = 3") == ("v2",)


def test_report_merge_ignores_the_staler_one(db):
    merge(db, "matchreports", MR_COLS,
          [(4, "{}", "{}", "v2", "f2", NEW)])
    merge(db, "matchreports", MR_COLS,
          [(4, "{}", "{}", "v1", "f1", OLD)])
    assert one(db, "SELECT model_version FROM matchreports"
                   " WHERE match_id = 4") == ("v2",)


def test_report_merge_covers_every_column(db):
    """Та же беда, что и у рангов, только заметнее по последствиям.

    Рукописный список колонок в этом слиянии и жил до спринта 156. Появись
    в MatchReports новая колонка — свежий отчёт втягивался бы со СТАРЫМ
    её значением, и отличить это от «отчёт не пересчитали» нельзя ничем.
    """
    with db.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = %s AND table_name = 'matchreports'"
                    " ORDER BY ordinal_position", (SCHEMA,))
        cols = [r[0] for r in cur.fetchall()]
    sql = merge_sql("matchreports", ",".join(cols))
    missing = [c for c in cols
               if c != "match_id" and f"{c} = EXCLUDED.{c}" not in sql]
    assert not missing, f"колонки не обновляются при слиянии: {missing}"
