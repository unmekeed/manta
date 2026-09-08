"""Каждый SQL шлюза обязан хотя бы РАЗОБРАТЬСЯ настоящим Postgres (198c).

ЖИВОЙ ОТКАЗ 08.09.2026, дважды подряд. Страница состояния сбора вернула
500 первым же вызовом:

    failed to encode args[0]: unable to encode 7 into text format
    ERROR: operator does not exist: date - double precision

Оба раза причина одна: параметр `$1` стоял в двух местах запроса, и
Postgres выводил для него тип, несовместимый со вторым местом. Оба раза
тесты были ЗЕЛЁНЫЕ и остались бы зелёными при любой ошибке в запросе.

ПОЧЕМУ ТАК ВЫШЛО. В спринте 198 логику страницы я намеренно вынес из SQL
в чистую функцию — чтобы её можно было проверить без базы, — и записал
это в комментарии как достоинство. Логика проверена. А сам SQL уехал в
production НИ РАЗУ НЕ ВЫПОЛНЕННЫМ: ни один тест его не касался, и
касаться было нечем.

ЧТО ДЕЛАЕТ ЭТОТ ФАЙЛ. Поднимает временный кластер Postgres, накатывает
НАСТОЯЩИЕ миграции проекта и делает PREPARE каждому SQL-запросу,
вынутому из исходников Go. PREPARE проверяет всё, что можно проверить без
данных: существование таблиц и колонок, синтаксис, выводимость типов
параметров. Ровно тот класс ошибок, что дважды доехал до боевой машины.

ЧЕГО ОН НЕ ПРОВЕРЯЕТ. Смысл запроса. Верно составленный, но неверно
задуманный SELECT пройдёт — за это отвечают тесты логики рядом.

СХЕМА БЕРЁТСЯ ИЗ МИГРАЦИЙ, А НЕ ПИШЕТСЯ ЗАНОВО. Рукописная копия схемы
разошлась бы с настоящей молча, и тест стал бы проверять несуществующую
базу — ровно то, чем этот проект уже болел (заглушки, которые проще
боевых клиентов).
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "infra" / "migrations" / "postgres"

# Откуда брать запросы. Список файлов явный: незнакомый файл с SQL должен
# попадать сюда осознанно, а не подхватываться и молча проверяться (или,
# что хуже, молча НЕ проверяться).
SQL_SOURCES = [
    ROOT / "apps" / "api-gateway" / "internal" / "handlers" / "admin_status.go",
]

# Go-константа вида `const somethingSQL = ` + обратная кавычка + текст.
SQL_CONST = re.compile(r"const\s+(\w*SQL)\s*=\s*`([^`]*)`", re.S)


def pg_bin() -> Path | None:
    """Каталог с бинарями Postgres; None — сервера на машине нет.

    Ищем именно СЕРВЕР, а не psql: клиент есть почти везде, а поднять
    кластер без initdb/postgres нельзя.
    """
    for base in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (base / "initdb").exists() and (base / "postgres").exists():
            return base
    found = shutil.which("initdb")
    return Path(found).parent if found else None


BIN = pg_bin()
pytestmark = pytest.mark.skipif(
    BIN is None,
    reason="сервер Postgres не установлен — PREPARE проверить негде")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Запуск от пользователя postgres, если мы root.

    Postgres отказывается работать от root, и это не наша прихоть.

    Смена пользователя делается параметром `user=`, а НЕ через `su -c`
    со склейкой команды в строку. Первая редакция склеивала — и первый же
    запрос с апострофами (`date_trunc('month', ...)`) приехал в psql без
    них: оболочка съела кавычки, а тест сообщил про ошибку синтаксиса,
    которой в исходнике не было. Обёртка, устроенная проще настоящего
    вызова, проверяет не то, что нужно, — ровно тот урок, что уже
    записан про заглушки.
    """
    if os.geteuid() == 0:
        kw.setdefault("user", "postgres")
        kw.setdefault("group", "postgres")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def db():
    """Временный кластер со схемой из настоящих миграций.

    Каталог в /tmp, а не в pytest tmp_path: сокет Unix упирается в права
    на промежуточные каталоги, и вложенный путь ломает подключение.
    """
    d = Path(tempfile.mkdtemp(prefix="manta-sql-", dir="/tmp"))
    try:
        if os.geteuid() == 0:
            shutil.chown(d, "postgres", "postgres")
        data = d / "data"
        r = run([str(BIN / "initdb"), "-D", str(data), "-A", "trust",
                 "-U", "postgres"])
        if r.returncode != 0:
            pytest.skip(f"initdb не отработал: {r.stderr[-300:]}")
        # listen_addresses пустой: только сокет, никаких портов наружу.
        r = run([str(BIN / "pg_ctl"), "-D", str(data), "-w",
                 "-o", f"-k {d} -c listen_addresses=", "-l", str(d / "log"),
                 "start"])
        if r.returncode != 0:
            pytest.skip(f"кластер не поднялся: {r.stderr[-300:]}")
        # Роли, которых миграции ждут от установки (create-db-users.sh):
        # 004 создаёт базу с OWNER dota, 011 раздаёт гранты остальным.
        # Заводим их здесь, а не переписываем миграции: схема обязана
        # накатываться так же, как на боевой машине.
        roles = ["dota", "manta_collector", "manta_gateway", "manta_reports",
                 "manta_ro"]
        r = run([str(BIN / "psql"), "-h", str(d), "-U", "postgres", "-q", "-c",
                 "; ".join(f"CREATE ROLE {r_} LOGIN" for r_ in roles)])
        assert r.returncode == 0, f"роли не созданы: {r.stderr[-300:]}"

        try:
            for sql in sorted(MIGRATIONS.glob("*.sql")):
                r = run([str(BIN / "psql"), "-h", str(d), "-U", "postgres",
                         "-v", "ON_ERROR_STOP=1", "-q", "-f", str(sql)])
                assert r.returncode == 0, (
                    f"миграция {sql.name} не накатилась: {r.stderr[-400:]}")
            yield d
        finally:
            run([str(BIN / "pg_ctl"), "-D", str(data), "-m", "immediate",
                 "stop"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def statements() -> list[tuple[str, str, str]]:
    """(файл, имя константы, текст запроса) из исходников Go."""
    out = []
    for path in SQL_SOURCES:
        src = path.read_text(encoding="utf-8")
        for name, body in SQL_CONST.findall(src):
            out.append((path.name, name, body.strip()))
    return out


def test_the_extraction_finds_the_queries():
    """Страховка от проверки пустоты.

    Сломайся разбор — параметризованный тест ниже получил бы пустой
    список и был бы тождественно зелёным. Этот проект уже ловил такое на
    себе, и не раз.
    """
    got = statements()
    assert len(got) >= 6, [n for _, n, _ in got]


def test_the_schema_really_has_the_tables(db):
    """Страховка вторая: миграции накатились, а не тихо ничего не создали.

    PREPARE к пустой базе падал бы на КАЖДОМ запросе, и разобрать, где
    ошибка в SQL, а где просто нет схемы, было бы невозможно.
    """
    r = run([str(BIN / "psql"), "-h", str(db), "-U", "postgres", "-tAc",
             "select count(*) from information_schema.tables "
             "where table_schema='public'"])
    assert r.returncode == 0, r.stderr
    assert int(r.stdout.strip()) >= 10, r.stdout


@pytest.mark.parametrize("where,name,sql", statements(),
                         ids=[f"{w}:{n}" for w, n, _ in statements()])
def test_every_query_prepares(db, where, name, sql):
    """ГЛАВНОЕ: запрос разбирается настоящим Postgres на настоящей схеме.

    PREPARE ловит несуществующую колонку, опечатку в имени таблицы,
    синтаксис и — то, на чём мы обожглись дважды, — невыводимый или
    противоречивый тип параметра. Всё это доезжало до боевой машины и
    возвращалось пятисоткой первому же посетителю.
    """
    r = run([str(BIN / "psql"), "-h", str(db), "-U", "postgres",
             "-v", "ON_ERROR_STOP=1", "-c", f"PREPARE t AS {sql}"])
    assert r.returncode == 0, (
        f"{where}:{name} не разбирается Postgres:\n{r.stderr.strip()}")


# -- параметр, использованный дважды -------------------------------------------

PARAM = re.compile(r"\$(\d+)\s*(::\s*\w+)?")


@pytest.mark.parametrize("where,name,sql", statements(),
                         ids=[f"{w}:{n}" for w, n, _ in statements()])
def test_a_repeated_parameter_declares_its_type_everywhere(where, name, sql):
    """Параметр в двух местах обязан быть приведён ЯВНО в каждом.

    ПОЧЕМУ ОДНОГО PREPARE МАЛО. Он проверяет запрос глазами сервера, а
    первая из двух боевых поломок была КЛИЕНТСКОЙ. `($1 || ' days')` с
    `$1::int` во второй половине Postgres принимает: он выводит параметр
    как text, а text→int приводится явно. PREPARE проходит. Падает уже
    pgx, когда пытается положить в text-параметр Go-шную семёрку:

        unable to encode 7 into text format for text (OID 25)

    Вторая поломка (`$1 * INTERVAL '1 day'` без приведения → double
    precision) PREPARE ловит. Значит два правила закрывают разные
    половины одной беды, и нужны оба.

    ПРАВИЛО. Если параметр встречается больше одного раза, вывод типа
    решать за нас не должен: в каждом месте стоит явное `::тип`. Тогда
    гадать нечего ни серверу, ни клиенту.

    Однократный параметр под правило не подпадает: там выводу неоткуда
    противоречить самому себе.
    """
    used: dict[str, list[str | None]] = {}
    for num, cast in PARAM.findall(sql):
        used.setdefault(num, []).append(
            cast.replace(" ", "") if cast else None)
    bad = {f"${n}": casts for n, casts in used.items()
           if len(casts) > 1 and (None in casts or len(set(casts)) > 1)}
    assert not bad, (
        f"{where}:{name}: параметр повторяется без единого явного типа — "
        f"{bad}. Вывод типа решит за нас, и разойдётся либо с соседним "
        f"местом запроса, либо с типом аргумента в Go")
