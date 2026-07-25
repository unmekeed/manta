#!/usr/bin/env bash
# Идемпотентный прогон ClickHouse-миграций через журнал (спринт 54).
#
# Инцидент: миграция 002 делает DROP TABLE ReplayEvents перед CREATE (это
# была одноразовая правка схемы). Но make recover прогонял migrate-ch на
# КАЖДОМ запуске (спринт 49), поэтому 002 стирала весь combat-лог при
# каждом recover. PositionSnapshots/EconomyTimeline/витрина создаются через
# IF NOT EXISTS и DROP над ними нет — уцелевали; терялся именно ReplayEvents,
# а с ним обучающие данные Death-Risk/Laning и атрибуция ошибок.
#
# Решение как у PG (scripts/pg-migrate.sh): каждый файл применяется РОВНО
# один раз, применённые запоминаются в manta.SchemaMigrations.
#
# Baseline на развёрнутой базе без журнала помечает применёнными ТОЛЬКО
# разрушительные файлы (с DROP TABLE) — их повторный прогон и есть баг.
# Остальные (CREATE/ALTER ... IF NOT EXISTS) идемпотентны и прогоняются
# честно: машина, отставшая на несколько миграций, доедет до актуальной
# схемы, а не получит фиктивно «применённые» записи в журнале.
set -euo pipefail
cd "$(dirname "$0")/.."

CH_DB="${CLICKHOUSE_DB:-manta}"
CLI=(docker exec -i manta-clickhouse-1 clickhouse-client
     --user "${CLICKHOUSE_USER:-dota}"
     --password "${CLICKHOUSE_PASSWORD:-dota_dev_password}")

q() { "${CLI[@]}" --database "$CH_DB" --query "$1"; }

q "CREATE TABLE IF NOT EXISTS SchemaMigrations (
     filename String, applied_at DateTime DEFAULT now())
   ENGINE = MergeTree() ORDER BY filename"

n_applied=$(q "SELECT count() FROM SchemaMigrations")
has_re=$(q "SELECT count() FROM system.tables
            WHERE database = '$CH_DB' AND name = 'ReplayEvents'")
if [ "$n_applied" = "0" ] && [ "$has_re" != "0" ]; then
    echo ">> CH: развёрнутая база без журнала — baseline разрушительных миграций"
    for f in infra/migrations/clickhouse/*.sql; do
        grep -q "DROP TABLE" "$f" || continue
        b=$(basename "$f")
        q "INSERT INTO SchemaMigrations (filename) VALUES ('$b')"
        echo "   baseline $b (содержит DROP — повторно не прогоняем)"
    done
fi

for f in infra/migrations/clickhouse/*.sql; do
    b=$(basename "$f")
    if [ "$(q "SELECT count() FROM SchemaMigrations WHERE filename = '$b'")" != "0" ]
    then
        echo "   $b — применена, пропуск"
        continue
    fi
    echo ">> $b"
    "${CLI[@]}" --multiquery < "$f"
    q "INSERT INTO SchemaMigrations (filename) VALUES ('$b')"
done
