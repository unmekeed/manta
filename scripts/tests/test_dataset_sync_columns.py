"""Обмен переживает расхождение схем между машинами (спринт 156).

ЧТО СЛУЧИЛОСЬ. Слепок Postgres выгружался как `\\copy таблица TO STDOUT
CSV`, а вливался как `COPY временная FROM STDIN CSV` — без списка колонок
ни там, ни там. Работает это ровно до первой миграции, добавившей
колонку: две машины почти никогда не стоят на одном коммите, и дамп с
четырьмя колонками, влитый в таблицу с пятью, даёт «missing data for
column». То есть обмен ломается при каждом изменении схемы и чинится
только одновременным обновлением обеих машин — а именно ради переезда на
новую машину он и существует.

Миграция 012 (has_replay) такую пару и развела.

ЧЕГО МАЛО. Мало просто перенести список колонок в архив. Колонка
has_replay значит «данные реплея у нас есть», по умолчанию она FALSE, и
импорт старого архива объявил бы все собранные реплеи несобранными —
принимающая машина пошла бы качать их заново, по 58 МиБ штука. Признак
надо ВОССТАНОВИТЬ из replay_url, тем же правилом, что и в самой миграции.

Здесь проверяется работа со строками — какие колонки уходят в COPY, какие
в INSERT и когда появляется восстановление. Смысл самих запросов живёт в
SQL и проверяется на живой базе:
apps/data-collector/tests/test_exchange_sql.py.
"""
import gzip
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "lib" / "dataset-tables.sh"
SYNC = ROOT / "scripts" / "dataset-sync.sh"

# Колонки CollectedMatches до миграции 012 и после. has_replay ПОСЛЕДНЯЯ —
# это и делает соблазнительным «отрезать хвост», а отрезав, теряешь
# настоящую колонку свежего архива.
OLD_CM = "match_id,source_name,replay_url,collected_at"
NEW_CM = OLD_CM + ",has_replay"


def sh(func: str, *args: str) -> str:
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {func} "$@"', "_", *args],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# -- разбор списка колонок ----------------------------------------------------

@pytest.mark.parametrize("col,cols,found", [
    ("has_replay", NEW_CM, True),
    ("has_replay", OLD_CM, False),
    ("match_id", NEW_CM, True),          # первая
    ("collected_at", NEW_CM, True),      # в середине
    ("replay", NEW_CM, False),           # часть имени — не имя
    ("has_replay_x", NEW_CM, False),     # и надстройка над именем тоже
    ("has_replay", "", False),
])
def test_column_lookup_matches_whole_names(col, cols, found):
    """Поиск по подстроке нашёл бы has_replay внутри has_replay_extra.

    Ложное «колонка есть» здесь тише всего: восстановление признака просто
    не сработает, импорт пройдёт успешно, и машина пойдёт качать реплеи
    заново — молча и за трафик.
    """
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; has_column "$1" "$2"', "_", col, cols])
    assert (r.returncode == 0) is found


# -- какие колонки куда -------------------------------------------------------

def test_old_archive_gets_the_column_added_for_insert():
    assert sh("pg_insert_cols", "collectedmatches", OLD_CM) == NEW_CM


def test_fresh_archive_is_left_alone():
    """И не удваивает колонку.

    `cols,has_replay` на свежем архиве дало бы «column has_replay
    specified more than once» — импорт упал бы целиком, причём именно на
    ПРАВИЛЬНОЙ паре машин.
    """
    assert sh("pg_insert_cols", "collectedmatches", NEW_CM) == NEW_CM


def test_other_tables_are_not_touched():
    """Правило узкое и именное.

    Соблазн написать его «для всех таблиц, где нет колонки» велик, но
    восстановить из соседних полей можно только has_replay — у остальных
    DEFAULT и есть правильный ответ.
    """
    ranks = "account_id,rank_tier,source,seen_count,seen_at,checked_at"
    assert sh("pg_insert_cols", "playerranks", ranks) == ranks
    assert sh("pg_backfill_sql", "playerranks", ranks, "tmp") == ""


# -- восстановление признака --------------------------------------------------

def test_backfill_appears_only_for_the_old_archive():
    assert sh("pg_backfill_sql", "collectedmatches", OLD_CM, "cm_tmp") != ""
    assert sh("pg_backfill_sql", "collectedmatches", NEW_CM, "cm_tmp") == ""


def test_backfill_repeats_the_rule_from_migration_012():
    """Правило одно и то же в двух местах, и разойтись им нельзя.

    json: — это матч, взятый по таймлайну (реплея нет); всё остальное —
    настоящая ссылка на реплей. Разъедься эти два места, и признак стал бы
    значить разное в зависимости от того, пришли данные сбором или
    обменом.
    """
    sql = sh("pg_backfill_sql", "collectedmatches", OLD_CM, "cm_tmp")
    assert "cm_tmp" in sql, "восстановление должно идти по ВРЕМЕННОЙ таблице"
    assert "has_replay = (replay_url NOT LIKE 'json:%')" in sql

    migration = (ROOT / "infra" / "migrations" / "postgres"
                 / "012_collected_has_replay.sql").read_text(encoding="utf-8")
    assert "replay_url NOT LIKE 'json:%'" in migration, (
        "правило в миграции изменилось — обмен должен измениться вместе с ним")


def test_backfill_targets_the_staging_table_not_the_real_one():
    """Промах здесь переписал бы ЖИВУЮ таблицу целиком.

    UPDATE без WHERE по collectedmatches назначил бы has_replay всем
    строкам разом — в том числе тем, чьи реплеи на этой машине никогда не
    скачивались.
    """
    sql = sh("pg_backfill_sql", "collectedmatches", OLD_CM, "cm_tmp")
    assert "UPDATE cm_tmp " in sql
    assert "UPDATE collectedmatches" not in sql


# -- архивы без списка колонок ------------------------------------------------

def _gz(tmp_path, text: str) -> str:
    p = tmp_path / "dump.csv.gz"
    p.write_bytes(gzip.compress(text.encode("utf-8")))
    return str(p)


def test_field_count_is_not_a_comma_count(tmp_path):
    """В matchreports лежат два JSONB.

    Запятых внутри одного поля там больше, чем полей, и подсчёт запятых
    дал бы число колонок «из воздуха» — а по нему берётся префикс схемы,
    то есть CSV лёг бы в ЧУЖИЕ колонки. Это не отказ, это перепутанные
    данные, и увидеть их можно только заглянув в таблицу.
    """
    row = '100,"{""a"": 1, ""b"": 2}","{""t"": [1, 2, 3]}",v1,f1,2026-08-10\n'
    assert row.count(",") == 8
    assert sh("csv_field_count", _gz(tmp_path, row)) == "6"


def test_field_count_survives_a_newline_inside_a_field(tmp_path):
    """Перевод строки внутри кавычек — часть записи, а не её конец."""
    row = '1,"строка\nс переводом",3\n'
    assert sh("csv_field_count", _gz(tmp_path, row)) == "3"


def test_field_count_handles_a_report_sized_field(tmp_path):
    """Поле в сотни килобайт — обычный отчёт, а не крайний случай.

    Разбор CSV по умолчанию отказывается читать поле длиннее 128 КиБ, и
    отказ этот тихий на вид: подсчёт возвращает пусто, импорт уходит в
    запасной путь и падает уже на COPY — с сообщением, которое про
    настоящую причину не говорит ничего.
    """
    big = "x" * 200_000
    row = f'100,"{{""a"": ""{big}""}}","{{}}",v1,f1,2026-08-10\n'
    assert sh("csv_field_count", _gz(tmp_path, row)) == "6"


def test_field_count_says_nothing_when_it_cannot_tell(tmp_path):
    """Пустой ответ — «не смог», и у вызывающего есть запасной путь.

    Ноль был бы хуже молчания: по нему взялся бы пустой список колонок, и
    COPY упал бы с синтаксической ошибкой вместо внятной причины.
    """
    assert sh("csv_field_count", _gz(tmp_path, "")) == ""


def test_archive_columns_takes_the_prefix_of_the_schema():
    """Старая схема — префикс новой: колонки только добавляются в конец."""
    assert sh("archive_columns", NEW_CM, "4") == OLD_CM
    assert sh("archive_columns", NEW_CM, "5") == NEW_CM


def test_archive_columns_refuses_a_wider_archive():
    """Полей больше, чем колонок, — домысливать нечего.

    Это архив от БОЛЕЕ НОВОЙ схемы, и молча взять первые пять колонок
    значило бы потерять шестую, ничего не сказав. Отказ здесь громче
    догадки.
    """
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; archive_columns "$1" "$2"', "_", NEW_CM, "6"],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert r.stdout.strip() == ""


# -- как этим пользуется скрипт -----------------------------------------------

def test_import_copies_by_archive_columns_and_inserts_by_full_ones():
    """Два списка не должны схлопнуться в один.

    COPY обязан читать РОВНО столько колонок, сколько в файле, а INSERT —
    вставлять и восстановленную. Возьми оба из одного места, и одна из
    двух половин сломается: либо «missing data for column» на COPY, либо
    потерянный признак на INSERT.
    """
    body = SYNC.read_text(encoding="utf-8")
    body = body[body.index("import_dataset()"):]
    assert 'COPY ${t}_import ($copy_cols) FROM STDIN CSV;' in body
    assert 'INSERT INTO $t ($cols) SELECT $cols FROM ${t}_import $merge;' in body
    assert 'copy_cols="$cols"' in body, "список COPY должен сниматься ДО правки"


def test_import_reads_the_archive_list_before_asking_the_schema():
    """Схема живой базы — запасной вариант, а не первый.

    Спроси импорт схему первой, и свежий архив со СВОИМ списком колонок
    вливался бы по чужой мерке — то есть починка обмена не работала бы
    ровно там, где её и чинили.
    """
    body = SYNC.read_text(encoding="utf-8")
    body = body[body.index("import_dataset()"):]
    from_archive = body.index('cols=$(cat "$dir/$t.cols"')
    from_schema = body.index('schema_cols=$(pg_columns "$t")')
    assert from_archive < from_schema
    assert 'if [ -z "$cols" ]; then' in body, (
        "схема должна спрашиваться только при пустом списке из архива")
    # И схема берётся НЕ ЦЕЛИКОМ, а обрезанной по числу полей в CSV.
    # Без этого старый архив вливался бы по нынешней схеме — то есть
    # ровно так, как он и не вливался до спринта 156.
    assert 'archive_columns "$schema_cols" "$fields"' in body, (
        "число полей архива посчитано, но не используется")


def test_export_writes_the_column_list_into_the_archive():
    """Без этого файла принимающая сторона снова гадает по своей схеме."""
    body = SYNC.read_text(encoding="utf-8")
    body = body[body.index("export_dataset()"):body.index("import_dataset()")]
    assert 'pg_columns "$t" >"$dir/$t.cols"' in body


def test_columns_come_from_the_declaration_order():
    """Порядок обязан совпасть с порядком колонок в CSV.

    \\copy выгружает колонки в порядке объявления. Спроси мы схему без
    ORDER BY ordinal_position — Postgres вернул бы их в порядке, который
    ничего не обещает, и CSV лёг бы в чужие колонки: не отказ, а
    перепутанные данные.
    """
    body = SYNC.read_text(encoding="utf-8")
    assert "ORDER BY ordinal_position" in body
