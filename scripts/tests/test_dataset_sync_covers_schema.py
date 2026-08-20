"""Слепок датасета не должен молча терять таблицы (спринты 149 и 156).

ЧТО СЛУЧИЛОСЬ ТРИЖДЫ В CLICKHOUSE. Списки таблиц написаны руками, а схема
живёт в миграциях. Расходятся они ровно тогда, когда схема меняется, — то
есть всегда, и заметить это нечем: экспорт проходит успешно, архив
выглядит полным, просто одной таблицы в нём нет.

    спринт 76   MatchDraft, MatchEvents  — появились с треком F,
                в список не внесли; слепок терял драфты и OOF-прайоры
    спринт 98   MatchMapCells            — то же самое, тепловые карты
    спринт 147  MatchMapCellsMinute      — то же самое, поминутные карты

Спринт 149 закрыл это проверкой — но только для ClickHouse. Postgres
списком не был вовсе: две таблицы стояли двумя строками кода, и проверка
их не видела.

ЧТО СЛУЧИЛОСЬ В POSTGRES. PlayerRanks — кэш рангов, который наполняется
квотой OpenDota (2000 запросов в сутки, купить больше нельзя) и копится
неделями. Он не переносился. Свежая машина получала полный слепок витрин
и пустой кэш; отбор кандидатов на ней не находил ни одного матча —
правило требует двух известных рангов из десяти игроков, а известных
рангов нет. Выглядело это как «очередь почему-то пустая». Вместе с ним не
переносились ReplayCandidates (та самая очередь) и ParkedReplays (матчи,
до которых не дошёл маршрут).

ЧТО ПРОВЕРЯЕТСЯ. Каждая таблица, создаваемая миграцией — обеих баз, —
должна быть названа в одном из списков scripts/lib/dataset-tables.sh:
переносимая или пропускаемая осознанно. Забыть новую таблицу теперь
нельзя молча — можно только явно, дописав её в список пропуска, и тогда
рядом придётся написать причину, потому что список читают глазами.
"""
import functools
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TABLES = ROOT / "scripts" / "lib" / "dataset-tables.sh"
MIGRATIONS = {
    "clickhouse": ROOT / "infra" / "migrations" / "clickhouse",
    "postgres": ROOT / "infra" / "migrations" / "postgres",
}


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


def schema_tables(dialect: str) -> set[str]:
    """Таблицы, создаваемые миграциями одной базы.

    Postgres складывает имена в нижний регистр: CREATE TABLE PlayerRanks
    заводит таблицу playerranks, и \\copy к ней обращается именно так.
    Сравнивать разные регистры — значит объявить забытой каждую таблицу
    сразу, то есть получить проверку, которая не может стать зелёной.
    """
    found: set[str] = set()
    for sql in sorted(MIGRATIONS[dialect].glob("*.sql")):
        found |= tables_in_sql(sql.read_text(encoding="utf-8"))
    return {t.lower() for t in found} if dialect == "postgres" else found


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


def test_postgres_names_are_folded_to_lower_case():
    """Регистр имени в миграции и в переносе разный — и это норма.

    Миграции пишут CREATE TABLE PlayerRanks, Postgres хранит playerranks,
    dataset-sync обращается к playerranks. Сравнивай мы как есть — ни одна
    таблица Postgres не считалась бы покрытой, и единственным способом
    сделать проверку зелёной было бы переписать регистр в одном из мест.
    """
    assert "playerranks" in schema_tables("postgres")
    assert "PlayerRanks" not in schema_tables("postgres")
    # А ClickHouse регистр СОХРАНЯЕТ: там имя таблицы чувствительно к нему.
    assert "MatchTimelineFeatures" in schema_tables("clickhouse")


@functools.lru_cache(maxsize=None)
def bash_list(name: str) -> frozenset:
    """Значения массива — спрошенные У BASH, а не вычитанные глазами.

    Разбор регулярным выражением здесь и стоял, и сломался ровно тогда,
    когда массив перестал быть плоским списком слов: REPLACING_TABLES
    начинается с «${MART_TABLES[@]}», и регулярка сочла это именем
    таблицы. Она видела ТЕКСТ, а скрипты работают со ЗНАЧЕНИЯМИ, и
    расходятся эти две картины на первой же живой конструкции языка.
    """
    r = subprocess.run(
        ["bash", "-c", f'. "{TABLES}"; printf "%s\\n" "${{{name}[@]}}"'],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return frozenset(r.stdout.split())


LISTS = {
    "clickhouse": ("REPLACING_TABLES", "RAW_TABLES", "SKIPPED_TABLES"),
    "postgres": ("PG_TABLES", "PG_SKIPPED_TABLES"),
}
CARRIED = {"clickhouse": ("REPLACING_TABLES", "RAW_TABLES"),
           "postgres": ("PG_TABLES",)}


def listed_tables(dialect: str) -> set[str]:
    return set().union(*(bash_list(n) for n in LISTS[dialect]))


def carried_tables(dialect: str) -> set[str]:
    return set().union(*(bash_list(n) for n in CARRIED[dialect]))


def test_the_parser_finds_something():
    """Страховка от проверки пустых множеств.

    Сломайся любой из двух разборов — и сравнение стало бы сравнением
    пустоты с пустотой, то есть вечно зелёным. Именно так эта проверка
    и отсутствовала последние три спринта: её не было вовсе, но
    выглядело бы это одинаково.
    """
    tables = schema_tables("clickhouse")
    assert len(tables) >= 10, f"разбор миграций нашёл только {tables}"
    assert "MatchTimelineFeatures" in tables and "ReplayEvents" in tables

    pg = schema_tables("postgres")
    assert len(pg) >= 10, f"разбор миграций Postgres нашёл только {pg}"
    assert "playerranks" in pg and "collectedmatches" in pg

    assert "MatchTimelineFeatures" in bash_list("REPLACING_TABLES")
    assert "PositionSnapshots" in bash_list("RAW_TABLES")
    assert "ReplayEvents" in bash_list("SKIPPED_TABLES")
    assert "collectedmatches" in bash_list("PG_TABLES")
    assert "collectorcursor" in bash_list("PG_SKIPPED_TABLES")


def test_multiline_bash_arrays_are_read_whole():
    """Разбор берёт ВЕСЬ массив, а не первую строку.

    REPLACING_TABLES занимает три строки, PG_SKIPPED_TABLES — тоже. Возьми
    разбор одну первую — забытыми объявились бы таблицы, которые на месте.
    Ложная тревога на верной конфигурации учит игнорировать проверку.
    """
    replacing = bash_list("REPLACING_TABLES")
    assert "MatchHeroTimings" in replacing, "последняя строка массива потеряна"
    assert len(replacing) >= 8
    assert "subscriptions" in bash_list("PG_SKIPPED_TABLES"), (
        "последняя строка PG_SKIPPED_TABLES потеряна")


def test_nested_array_references_are_expanded():
    """REPLACING_TABLES начинается с раскрытия другого массива.

    Витрины перечислены отдельно (их показывает своя строка отчёта), а в
    перенос входят вместе со всеми — через «${MART_TABLES[@]}». Читай
    проверка текст файла, она увидела бы здесь имя таблицы «${MART_...}»
    и потребовала бы найти его в схеме, а настоящие витрины сочла бы
    забытыми: тревога на верной конфигурации, причём сразу двойная.
    """
    assert "MatchTimelineFeatures" in bash_list("REPLACING_TABLES")
    assert not [t for t in bash_list("REPLACING_TABLES") if "$" in t]


@pytest.mark.parametrize("dialect,table", [
    (d, t) for d in sorted(MIGRATIONS) for t in sorted(schema_tables(d))])
def test_every_schema_table_is_accounted_for(dialect, table):
    """Каждая таблица схемы либо переносится, либо пропущена ОСОЗНАННО."""
    assert table in listed_tables(dialect), (
        f"{table} создаётся миграцией {dialect}, но не названа ни в одном "
        f"из списков {LISTS[dialect]} — слепок потеряет её молча")


@pytest.mark.parametrize("dialect", sorted(MIGRATIONS))
def test_lists_do_not_mention_tables_that_do_not_exist(dialect):
    """И наоборот: в списке нет таблиц, которых нет в схеме.

    Опечатка в имени не падает при экспорте — dataset-sync складывает
    дампы по одному, и промах даёт пустой файл, неотличимый от пустой
    таблицы. Переименуй кто-нибудь таблицу миграцией — и слепок начнёт
    терять её, оставаясь зелёным.
    """
    ghosts = sorted(listed_tables(dialect) - schema_tables(dialect))
    assert not ghosts, (
        f"в списках переноса ({dialect}) есть таблицы, которых нет в схеме: "
        f"{ghosts}")


@pytest.mark.parametrize("dialect", sorted(MIGRATIONS))
def test_a_table_is_not_in_two_lists_at_once(dialect):
    """Переносимая и пропускаемая одновременно — противоречие.

    Оно молчит: скрипт перенесёт её (список пропуска он не читает), а
    читающий решит, что не перенесёт.
    """
    skipped = LISTS[dialect][-1]
    both = carried_tables(dialect) & bash_list(skipped)
    assert not both, f"таблица и переносится, и пропускается: {sorted(both)}"


def test_map_cells_minute_is_carried():
    """Именно та таблица, из-за которой случился спринт 149.

    Проверка выше поймала бы её и без этого теста, но она параметрическая:
    выпади разбор миграций — и параметров не станет вовсе, а pytest
    сочтёт это успехом. Здесь имя записано прямо.
    """
    assert "MatchMapCellsMinute" in bash_list("REPLACING_TABLES")


def test_the_rank_cache_is_carried():
    """И та, из-за которой случился спринт 156.

    Кэш рангов стоит квоты OpenDota и копится неделями; из сырья он не
    восстанавливается ничем, кроме той же квоты. Без него отбор
    кандидатов на новой машине не находит ни одного матча.
    """
    assert "playerranks" in bash_list("PG_TABLES")
    assert "replaycandidates" in bash_list("PG_TABLES")


@pytest.mark.parametrize("table", ["apibudget", "collectorcursor"])
def test_machine_local_state_is_not_carried(table):
    """Обратная сторона: перенести можно ЛИШНЕЕ, и это хуже забытого.

    apibudget — суточный расход квоты, а квота считается НА IP. Втянув
    чужой расход, машина решит, что запросы уже потрачены, и перестанет
    собирать — молча, потому что для неё это законное «квота кончилась».
    collectorcursor — позиция в потоке Valve, чужая отбросит сбор.
    """
    assert table not in bash_list("PG_TABLES"), (
        f"{table} — машинно-специфичное состояние, переносить его вредно")
    assert table in bash_list("PG_SKIPPED_TABLES"), (
        f"{table} должна быть в списке пропуска с объяснением")


# -- один список на весь проект -----------------------------------------------

SCRIPTS = ROOT / "scripts"


def test_no_script_keeps_its_own_table_list():
    """Списки объявлены ровно в одном файле.

    До спринта 156 их было два: dataset-sync.sh знал, что выгружать, а
    backup-drill.sh — что сверять после восстановления. Совпадать они
    обязаны, сверялись только глазами, и разъехались: учения проверяли
    девять таблиц ClickHouse из десяти. Учения, не знающие о таблице,
    объявляют её восстановленной — то есть проходят на потерянных данных.
    """
    offenders = []
    for sh in sorted(SCRIPTS.glob("*.sh")):
        text = sh.read_text(encoding="utf-8")
        for name in ("REPLACING_TABLES", "RAW_TABLES", "SKIPPED_TABLES",
                     "PG_TABLES", "PG_SKIPPED_TABLES"):
            if re.search(rf"^{name}=\(", text, re.M):
                offenders.append(f"{sh.name}:{name}")
    assert not offenders, (
        "списки таблиц объявлены не только в lib/dataset-tables.sh: "
        f"{offenders}")


def test_no_script_walks_a_literal_list_of_tables():
    """Список можно не только объявить заново, но и вписать прямо в цикл.

    Мутационная проверка вскрыла это здесь же: замена
    `for t in "${PG_TABLES[@]}"` на `for t in collectedmatches` прошла
    мимо всех проверок. Учения при этом сверяют одну таблицу из пяти и
    бодро печатают «УЧЕНИЯ ПРОЙДЕНЫ» — то есть объявляют восстановленным
    то, что потеряно.

    Правило простое: цикл по таблицам обязан разворачивать массив.
    """
    known = {t.lower() for t in
             (listed_tables("clickhouse") | listed_tables("postgres"))}
    offenders = []
    for sh in sorted(SCRIPTS.glob("*.sh")):
        for n, line in enumerate(sh.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r"^\s*for\s+\w+\s+in\s+(.+?)\s*;\s*do", line)
            if not m or "[@]" in m.group(1):
                continue
            if {w.strip('"').lower() for w in m.group(1).split()} & known:
                offenders.append(f"{sh.name}:{n}: {line.strip()}")
    assert not offenders, (
        "цикл по таблицам перечисляет их вручную — список разъедется:\n"
        + "\n".join(offenders))


@pytest.mark.parametrize("script", ["dataset-sync.sh", "backup-drill.sh"])
def test_scripts_actually_source_the_shared_lists(script):
    """И пользуются ими на самом деле.

    Проверка выше запрещает объявлять список заново; без этой она
    поощряла бы просто выбросить его — и скрипт остался бы с пустым
    массивом, то есть перестал бы переносить (или сверять) хоть что-то,
    не сказав ни слова.
    """
    r = subprocess.run(
        ["bash", "-c",
         f'. "{SCRIPTS}/lib/dataset-tables.sh"; '
         'echo "${PG_TABLES[*]} ${REPLACING_TABLES[*]}"'],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "playerranks" in r.stdout and "MatchFights" in r.stdout

    text = (SCRIPTS / script).read_text(encoding="utf-8")
    assert "lib/dataset-tables.sh" in text, (
        f"{script} не подключает общий список таблиц")
    assert "PG_TABLES[@]" in text, (
        f"{script} подключает список, но не использует его")
