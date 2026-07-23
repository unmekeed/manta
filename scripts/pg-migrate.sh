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

export PGPASSWORD="${PGPASSWORD:-dota_dev_password}"
PSQL=(psql -h "${POSTGRES_HOST:-localhost}" -U "${POSTGRES_USER:-dota}"
      -d "${POSTGRES_DB:-manta}" -v ON_ERROR_STOP=1 -qtA)

# Файлы, существовавшие до появления журнала — только их можно баселайнить.
BASELINE="001_init.sql 002_outbox.sql 003_reports.sql 004_mlflow_database.sql"

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS SchemaMigrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"

n_applied=$("${PSQL[@]}" -c "SELECT count(*) FROM SchemaMigrations")
has_outbox=$("${PSQL[@]}" -c \
    "SELECT count(*) FROM pg_tables WHERE tablename = 'eventoutbox'")
if [ "$n_applied" = "0" ] && [ "$has_outbox" != "0" ]; then
    echo ">> база уже развёрнута, журнала нет — baseline: $BASELINE"
    for b in $BASELINE; do
        "${PSQL[@]}" -c "INSERT INTO SchemaMigrations(filename)
                         VALUES ('$b') ON CONFLICT DO NOTHING"
    done
fi

for f in infra/migrations/postgres/*.sql; do
    b=$(basename "$f")
    if [ "$("${PSQL[@]}" -c \
        "SELECT count(*) FROM SchemaMigrations WHERE filename = '$b'")" != "0" ]
    then
        echo "   $b — применена, пропуск"
        continue
    fi
    echo ">> $b"
    "${PSQL[@]}" -f "$f"
    "${PSQL[@]}" -c "INSERT INTO SchemaMigrations(filename) VALUES ('$b')"
done
