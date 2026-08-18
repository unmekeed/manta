#!/usr/bin/env bash
# Идемпотентный прогон миграций Postgres (спринт 49, инцидент №7 HANDOFF:
# «после git pull забыли make migrate»). Каждый файл применяется РОВНО один
# раз: применённые запоминаются в таблице SchemaMigrations, повторный запуск
# безопасен и прогоняет только новые файлы.
#
# Старые миграции (001–004) сами по себе НЕ идемпотентны (CREATE TYPE/TABLE
# без IF NOT EXISTS) — на уже развёрнутой базе без журнала (признак:
# существует таблица eventoutbox) baseline-список помечается применённым
# без прогона. Новые миграции (005+) попадают в журнал обычным путём.
set -euo pipefail
cd "$(dirname "$0")/.."

# psql запускается ВНУТРИ контейнера, а не на хосте (спринт 143).
#
# Раньше скрипт звал хостовый psql. На домашней машине клиент стоял, и это
# не всплывало; на чистом VPS развёртывание встало на «psql: command not
# found» — уже после того, как весь стек поднялся и ClickHouse ответил.
# Образ postgres клиент содержит, так что зависимости от хоста не нужно
# вовсе: сосед по задаче, ch-migrate.sh, так и делал с самого начала.
#
# Контейнер параметризован по той же причине, что и в ch-migrate.sh:
# учения по восстановлению (backup-drill.sh) накатывают схему на ВРЕМЕННУЮ
# базу этим же кодом.
PG_CONTAINER="${PG_CONTAINER:-manta-postgres-1}"
PGPASS="${PGPASSWORD:-${POSTGRES_PASSWORD:-dota_dev_password}}"
PSQL=(docker exec -i -e "PGPASSWORD=$PGPASS" "$PG_CONTAINER"
      psql -U "${POSTGRES_USER:-dota}"
      -d "${POSTGRES_DB:-manta}" -v ON_ERROR_STOP=1 -qtA)

docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER" || {
    echo "ОСТАНОВ: контейнер $PG_CONTAINER не запущен — накатывать некуда" >&2
    exit 1
}

# Файлы, существовавшие до появления журнала — только их можно баселайнить.
BASELINE="001_init.sql 002_outbox.sql 003_reports.sql 004_mlflow_database.sql"

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS SchemaMigrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())" </dev/null

n_applied=$("${PSQL[@]}" -c "SELECT count(*) FROM SchemaMigrations" </dev/null)
has_outbox=$("${PSQL[@]}" -c \
    "SELECT count(*) FROM pg_tables WHERE tablename = 'eventoutbox'" </dev/null)
if [ "$n_applied" = "0" ] && [ "$has_outbox" != "0" ]; then
    echo ">> база уже развёрнута, журнала нет — baseline: $BASELINE"
    for b in $BASELINE; do
        "${PSQL[@]}" -c "INSERT INTO SchemaMigrations(filename)
                         VALUES ('$b') ON CONFLICT DO NOTHING" </dev/null
    done
fi

for f in infra/migrations/postgres/*.sql; do
    b=$(basename "$f")
    if [ "$("${PSQL[@]}" -c \
        "SELECT count(*) FROM SchemaMigrations WHERE filename = '$b'" </dev/null)" != "0" ]
    then
        echo "   $b — применена, пропуск"
        continue
    fi
    echo ">> $b"
    # Файл лежит на хосте, а psql — внутри контейнера, поэтому -f
    # неприменим: подаём содержимое через stdin.
    "${PSQL[@]}" <"$f"
    "${PSQL[@]}" -c "INSERT INTO SchemaMigrations(filename) VALUES ('$b')" </dev/null
done
