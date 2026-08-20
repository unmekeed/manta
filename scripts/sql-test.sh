#!/usr/bin/env bash
# Прогон SQL-тестов на ЖИВОЙ базе — в образе, а не на хосте (спринт 152).
#
# ЗАЧЕМ. Часть проверок бессмысленна без настоящего Postgres: смысл живёт
# в предикате запроса, а фейковый курсор возвращает то, что в него
# положили, и любая мутация предиката проходит мимо. Такие тесты в
# проекте есть (дедуп по возможностям, точность отбора кандидатов), но
# запускались они командой, которую приходилось собирать по памяти, — и
# трижды подряд собиралась она неверно:
#
#   make            на VPS не установлен вовсе
#   pytest          на хосте нет и быть не должно: всё живёт в контейнерах
#   python:3.11-slim голый образ падал на импорте confluent_kafka
#
# Тот же урок, что и у миграций в спринте 143: инструмент надо брать
# оттуда, где он уже есть. Образ коллектора содержит и psycopg, и
# requests, и confluent_kafka; не хватает только pytest, и он ставится
# в одноразовый контейнер.
#
# Пароль читается ИЗ ОКРУЖЕНИЯ КОНТЕЙНЕРА, а не из env-файла: файл на
# машине может содержать несколько строк с одним именем, разные имена для
# одного значения или не содержать ничего — всё это уже случалось. У
# запущенного Postgres пароль ровно один, и это тот, который он принимает.
#
# Использование:
#   ./scripts/sql-test.sh                      # все *_sql.py коллектора
#   ./scripts/sql-test.sh tests/test_dedup_sql.py
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-manta-postgres-1}"
APP_CONTAINER="${APP_CONTAINER:-manta-data-collector-1}"
APP_DIR="${APP_DIR:-apps/data-collector}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { printf 'ОСТАНОВ: %s\n' "$1" >&2; exit 1; }

command -v docker >/dev/null || die "docker не установлен — база живёт в нём"

docker inspect "$PG_CONTAINER" >/dev/null 2>&1 \
    || die "контейнер $PG_CONTAINER не найден: подними стек или задай PG_CONTAINER"
docker inspect "$APP_CONTAINER" >/dev/null 2>&1 \
    || die "контейнер $APP_CONTAINER не найден: из него берётся образ с зависимостями"

pg_env() { docker exec "$PG_CONTAINER" printenv "$1" 2>/dev/null || true; }

PG_USER="$(pg_env POSTGRES_USER)"
PG_PASS="$(pg_env POSTGRES_PASSWORD)"
PG_DB="$(pg_env POSTGRES_DB)"
[ -n "$PG_PASS" ] || die "у $PG_CONTAINER нет POSTGRES_PASSWORD в окружении"

# Сеть берём у самого Postgres и ходим по имени контейнера. Через
# опубликованный порт было бы короче, но на VPS он привязан к 127.0.0.1, а
# --network host ведёт себя по-разному на разных установках docker.
NET="$(docker inspect -f \
    '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
    "$PG_CONTAINER" | head -1)"
[ -n "$NET" ] || die "у $PG_CONTAINER не видно сети"

IMG="$(docker inspect -f '{{.Config.Image}}' "$APP_CONTAINER")"
[ -n "$IMG" ] || die "не удалось определить образ у $APP_CONTAINER"

TESTS=("$@")
if [ ${#TESTS[@]} -eq 0 ]; then
    # Имена не записаны списком: новый SQL-тест обязан подхватываться сам,
    # иначе он тихо не будет запускаться никогда.
    mapfile -t TESTS < <(cd "$REPO/$APP_DIR" && ls tests/test_*_sql.py 2>/dev/null)
    [ ${#TESTS[@]} -gt 0 ] || die "в $APP_DIR/tests нет ни одного test_*_sql.py"
fi

printf '>> SQL-тесты на живой базе\n'
printf '   образ:    %s\n' "$IMG"
printf '   сеть:     %s\n' "$NET"
printf '   тесты:    %s\n' "${TESTS[*]}"

# --user root: образ работает под nobody, и pip иначе не запишет.
# Каталог репозитория монтируется целиком — тестам нужен и libs, и
# infra/migrations, откуда берётся настоящая схема.
docker run --rm --user root --network "$NET" \
    -v "$REPO:/repo" -w "/repo/$APP_DIR" \
    -e MANTA_TEST_DSN="postgresql://${PG_USER:-dota}:${PG_PASS}@${PG_CONTAINER}:5432/${PG_DB:-manta}" \
    -e PYTHONPATH="/repo/$APP_DIR/src:/repo/libs" \
    -e PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint sh "$IMG" \
    -c "pip install -q pytest >/dev/null && python -m pytest ${TESTS[*]} -q"
