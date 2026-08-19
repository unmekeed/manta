"""Слепок датасета не должен молча терять таблицы (спринт 149).

ЧТО СЛУЧИЛОСЬ ТРИЖДЫ. Списки таблиц в scripts/dataset-sync.sh написаны
руками, а схема живёт в миграциях. Расходятся они ровно тогда, когда
схема меняется, — то есть всегда, и заметить это нечем: экспорт проходит
успешно, архив выглядит полным, просто одной таблицы в нём нет.

    спринт 76   MatchDraft, MatchEvents  — появились с треком F,
                в список не внесли; слепок терял драфты и OOF-прайоры
    спринт 98   MatchMapCells            — то же самое, тепловые карты
    спринт 147  MatchMapCellsMinute      — то же самое, поминутные карты

Каждый раз пропажу замечали ПОСЛЕ, и каждый раз в комментарий дописывали
объяснение, почему эту таблицу забывать нельзя. Три объяснения в одном
файле — и ни одной проверки.

Цена ошибки не «неудобно». Агрегаты собираются из ReplayEvents и
PositionSnapshots; первое живёт 14 дней по TTL, реплеи Valve удаляет
сама. Таблица, не попавшая в слепок, теряется БЕЗВОЗВРАТНО при переносе
на другую машину — в отличие от витрин, которые пересчитываются из сырья.

ЧТО ПРОВЕРЯЕТСЯ. Каждая таблица, создаваемая миграцией ClickHouse, должна
быть названа в одном из трёх списков: переносимая (двух видов) или
пропускаемая осознанно. Забыть новую таблицу теперь нельзя молча — можно
только явно, дописав её в SKIPPED_TABLES, и тогда рядом придётся написать
причину, потому что список читают глазами.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC = ROOT / "scripts" / "dataset-sync.sh"
MIGRATIONS = ROOT / "infra" / "migrations" / "clickhouse"


CREATE_RE = re.compile(
    r"CREATE\s+(?:TABLE|MATERIALIZED\s+VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)", re.I)


def tables_in_sql(text: str) -> set[str]:
    """Таблицы, создаваемые этим SQL. Комментарии не считаются.

    Разбор вынесен из чтения файлов НАРОЧНО. Пока он умел читать только
    живые миграции, проверить вырезание комментариев было не на чем: ни в
    одной из них слов CREATE TABLE в пояснении нет, и мутация «не
    вырезать комментарии» проходила незамеченной. Ровно так же в спринте
    143 пережил проверку разбор requirements.txt.

    Случай не выдуманный: комментарии в этих миграциях подробные и
    ссылаются на соседние таблицы. Первое же пояснение вида «в отличие от
    CREATE TABLE manta.Foo» завело бы призрачную таблицу — и проверка
    начала бы требовать внести её в перенос, то есть подняла бы ложную
    тревогу на верной схеме.
    """
    body = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("--"))
    return set(CREATE_RE.findall(body))


def schema_tables() -> set[str]:
    """Таблицы, создаваемые миграциями ClickHouse."""
    found: set[str] = set()
    for sql in sorted(MIGRATIONS.glob("*.sql")):
        found |= tables_in_sql(sql.read_text(encoding="utf-8"))
    return found


def test_comments_do_not_create_phantom_tables():
    """Пояснение — не объявление.

    Без вырезания комментариев упоминание таблицы в прозе становилось бы
    объявлением, и проверка требовала бы внести в перенос то, чего нет.
    """
    got = tables_in_sql(
        "-- В отличие от CREATE TABLE manta.Ghost, здесь ReplacingMergeTree.\n"
        "CREATE TABLE IF NOT EXISTS manta.Real (a UInt8) ENGINE = Memory;\n")
    assert got == {"Real"}


def test_parser_understands_the_forms_used_in_migrations():
    """Оба написания и оба вида объекта.

    Промах здесь не падает: таблица просто не попадёт в множество, и
    проверка объявит схему полностью покрытой.
    """
    assert tables_in_sql("CREATE TABLE manta.A (x UInt8)") == {"A"}
    assert tables_in_sql("CREATE TABLE IF NOT EXISTS manta.B (x UInt8)") == {"B"}
    assert tables_in_sql("create table manta.C (x UInt8)") == {"C"}
    assert tables_in_sql("CREATE MATERIALIZED VIEW manta.D TO manta.A AS SELECT 1") == {"D"}
    assert tables_in_sql("ALTER TABLE manta.A MODIFY COLUMN x UInt16") == set()


def bash_list(name: str) -> set[str]:
    """Значения bash-массива ИМЯ=(a b c), в том числе многострочного."""
    text = SYNC.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}=\((.*?)\)", text, re.S | re.M)
    if not m:
        return set()
    body = "\n".join(l.split("#", 1)[0] for l in m.group(1).splitlines())
    return set(body.split())


def listed_tables() -> set[str]:
    return (bash_list("REPLACING_TABLES") | bash_list("RAW_TABLES")
            | bash_list("SKIPPED_TABLES"))


def test_the_parser_finds_something():
    """Страховка от проверки пустых множеств.

    Сломайся любой из двух разборов — и сравнение стало бы сравнением
    пустоты с пустотой, то есть вечно зелёным. Именно так эта проверка
    и отсутствовала последние три спринта: её не было вовсе, но
    выглядело бы это одинаково.
    """
    tables = schema_tables()
    assert len(tables) >= 10, f"разбор миграций нашёл только {tables}"
    assert "MatchTimelineFeatures" in tables and "ReplayEvents" in tables

    assert "MatchTimelineFeatures" in bash_list("REPLACING_TABLES")
    assert "PositionSnapshots" in bash_list("RAW_TABLES")
    assert "ReplayEvents" in bash_list("SKIPPED_TABLES")


def test_multiline_bash_arrays_are_parsed_whole():
    """Разбор берёт ВЕСЬ массив, а не первую строку.

    REPLACING_TABLES занимает две строки, и разбор до перевода строки
    нашёл бы половину имён — то есть объявил бы забытыми таблицы,
    которые на месте. Ложная тревога на верной конфигурации учит
    игнорировать проверку.
    """
    replacing = bash_list("REPLACING_TABLES")
    assert "MatchHeroTimings" in replacing, "вторая строка массива потеряна"
    assert len(replacing) >= 8


@pytest.mark.parametrize("table", sorted(schema_tables()))
def test_every_schema_table_is_accounted_for(table):
    """Каждая таблица схемы либо переносится, либо пропущена ОСОЗНАННО."""
    assert table in listed_tables(), (
        f"{table} создаётся миграцией, но не названа ни в REPLACING_TABLES, "
        f"ни в RAW_TABLES, ни в SKIPPED_TABLES — слепок потеряет её молча")


def test_lists_do_not_mention_tables_that_do_not_exist():
    """И наоборот: в списке нет таблиц, которых нет в схеме.

    Опечатка в имени не падает при экспорте — dataset-sync складывает
    дампы по одному, и промах даёт пустой файл, неотличимый от пустой
    таблицы. Переименуй кто-нибудь таблицу миграцией — и слепок начнёт
    терять её, оставаясь зелёным.
    """
    ghosts = sorted(listed_tables() - schema_tables())
    assert not ghosts, (
        f"в списках переноса есть таблицы, которых нет в схеме: {ghosts}")


def test_a_table_is_not_in_two_lists_at_once():
    """Переносимая и пропускаемая одновременно — противоречие.

    Оно молчит: скрипт перенесёт её (список пропуска он не читает), а
    читающий решит, что не перенесёт.
    """
    both = ((bash_list("REPLACING_TABLES") | bash_list("RAW_TABLES"))
            & bash_list("SKIPPED_TABLES"))
    assert not both, f"таблица и переносится, и пропускается: {sorted(both)}"


def test_map_cells_minute_is_carried():
    """Именно та таблица, из-за которой спринт и случился.

    Проверка выше поймала бы её и без этого теста, но она параметрическая:
    выпади разбор миграций — и параметров не станет вовсе, а pytest
    сочтёт это успехом. Здесь имя записано прямо.
    """
    assert "MatchMapCellsMinute" in bash_list("REPLACING_TABLES")
