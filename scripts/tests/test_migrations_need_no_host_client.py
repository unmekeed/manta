"""Миграции не должны требовать клиентов БД на хосте (спринт 143).

ЧТО СЛУЧИЛОСЬ. Развёртывание на чистом VPS дошло до конца — стек поднят,
ClickHouse отвечает — и встало на

    ./scripts/pg-migrate.sh: line 22: psql: command not found

Скрипт звал ХОСТОВЫЙ psql. На домашней машине клиент стоял, поэтому за
десятки прогонов это ни разу не всплыло. Соседний ch-migrate.sh с самого
начала работал через `docker exec` и на VPS отработал без единой правки —
то есть правильный образец лежал рядом.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ ТЕСТ. Зависимость от хоста не видна ни в одном
прогоне на машине, где клиент есть. Она вскрывается только там, где
дороже всего: на чужой машине, в середине установки, когда всё остальное
уже поднято.

Правило простое: образы Postgres и ClickHouse содержат свои клиенты,
поэтому хостовые не нужны никому.
"""
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
MIGRATORS = sorted(SCRIPTS.glob("*-migrate.sh"))

# Клиенты, которых на чистой машине нет и быть не должно.
HOST_CLIENTS = ("psql", "clickhouse-client", "pg_dump", "pg_restore", "mysql")


def code_of(path: Path) -> str:
    """Скрипт без комментариев: пояснение про psql — не вызов psql."""
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


def logical_lines(code: str) -> list[str]:
    """Склеить продолжения строк в одну команду.

    Вызов клиента живёт в массиве, растянутом на несколько строк:

        PSQL=(docker exec -i ... "$PG_CONTAINER"
              psql -U ...)

    Построчная проверка видит здесь «psql» без «docker exec» рядом и
    поднимает ложную тревогу на совершенно правильном коде. Логическая
    строка кончается там, где закрыты скобки и нет переноса обратным
    слэшем.
    """
    out, buf, depth = [], "", 0
    for line in code.splitlines():
        buf = f"{buf} {line.strip()}" if buf else line
        depth += line.count("(") - line.count(")")
        if depth <= 0 and not line.rstrip().endswith("\\"):
            out.append(buf)
            buf, depth = "", 0
    if buf:
        out.append(buf)
    return out


def test_there_are_migrators_to_check():
    """Страховка от проверки пустого списка."""
    assert len(MIGRATORS) >= 2, f"миграторов найдено: {[m.name for m in MIGRATORS]}"


def test_no_migrator_calls_a_database_client_on_the_host():
    """Клиент БД вызывается только ВНУТРИ контейнера.

    Ищем вызовы клиента, перед которыми нет `docker exec`. Именно так
    выглядела поломка: `PSQL=(psql -h localhost ...)`.
    """
    bad = []
    for path in MIGRATORS:
        for line in logical_lines(code_of(path)):
            for client in HOST_CLIENTS:
                # Слово целиком: `clickhouse-client` не должен ловиться
                # как `psql`, а имя переменной CLICKHOUSE_PASSWORD — как
                # вызов клиента.
                if not re.search(rf"(^|[\s(=]){re.escape(client)}(\s|$)", line):
                    continue
                if "docker exec" in line:
                    continue
                bad.append(f"{path.name}: {line.strip()}")
    assert not bad, (
        "клиент БД вызывается на хосте — на чистой машине его нет:\n"
        + "\n".join(bad))


def test_every_migrator_targets_a_parameterised_container():
    """Имя контейнера — переменная, а не литерал.

    Учения по восстановлению накатывают схему на ВРЕМЕННЫЕ базы этим же
    кодом. Зашитое имя означало бы, что учения идут в боевую базу.
    """
    for path in MIGRATORS:
        code = code_of(path)
        assert re.search(r"_CONTAINER=\"\$\{[A-Z_]+:-manta-", code), \
            f"{path.name}: контейнер не параметризован"


def test_every_migrator_says_so_when_the_container_is_missing():
    """Нет контейнера — внятный отказ, а не `docker exec` в пустоту.

    Без проверки ошибка выглядит как «Error: No such container» посреди
    вывода, и непонятно, чей это контейнер и почему его ждали.
    """
    for path in MIGRATORS:
        code = code_of(path)
        assert "docker ps" in code, \
            f"{path.name}: не проверяет, что контейнер вообще запущен"
