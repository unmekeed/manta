#!/usr/bin/env bash
# Синхронизация датасета между машинами (E2 роадмапа: облако ↔ локалка).
#
#   ./scripts/dataset-sync.sh export [файл.tar]   # снять слепок датасета
#   ./scripts/dataset-sync.sh import файл.tar     # идемпотентно влить слепок
#
# Что переносится:
#   ClickHouse: MatchTimelineFeatures, PlayerMatchFeatures (витрины,
#     ReplacingMergeTree — повторная вставка дедуплицируется движком),
#     EconomyTimeline, PositionSnapshots (сырьё для laning/SI/отчётов,
#     MergeTree — при импорте вливаются только новые match_id через
#     staging-таблицу). SKIP_RAW=1 — пропустить сырьё (архив меньше).
#   PostgreSQL: collectedmatches (дедуп коллекторов — БЕЗ него вторая
#     машина заново скачивала бы те же матчи), matchreports (готовые
#     отчёты; при конфликте побеждает более свежий generated_at).
#
# ReplayEvents не переносится: 10^7+ строк, TTL 14 дней, регенерируется
# парсером из реплеев. Курсоры коллекторов машинно-специфичны.
#
# Импорт можно повторять сколько угодно: счётчики не растут.
set -euo pipefail

CH="${CH_CONTAINER:-manta-clickhouse-1}"
PG="${PG_CONTAINER:-manta-postgres-1}"
CH_USER="${CLICKHOUSE_USER:-dota}"
CH_PASS="${CLICKHOUSE_PASSWORD:-dota_dev_password}"
PG_USER="${POSTGRES_USER:-dota}"
PG_DB="${POSTGRES_DB:-manta}"

REPLACING_TABLES=(MatchTimelineFeatures PlayerMatchFeatures)
RAW_TABLES=(EconomyTimeline PositionSnapshots)

chq() { docker exec -i "$CH" clickhouse-client --user "$CH_USER" --password "$CH_PASS" -q "$1"; }
pgq() { docker exec -i "$PG" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -q "$@"; }

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,15p'; exit 1; }

export_dataset() {
    local out="${1:-manta-dataset-$(date -u +%Y%m%dT%H%M).tar}"
    local dir; dir=$(mktemp -d)
    trap "rm -rf '$dir'" EXIT

    local tables=("${REPLACING_TABLES[@]}")
    [ "${SKIP_RAW:-}" = "1" ] || tables+=("${RAW_TABLES[@]}")

    for t in "${tables[@]}"; do
        echo ">> CH $t"
        chq "SELECT * FROM manta.$t FORMAT Native" | gzip >"$dir/$t.native.gz"
    done

    echo ">> PG collectedmatches"
    pgq -c "\\copy collectedmatches TO STDOUT CSV" | gzip >"$dir/collectedmatches.csv.gz"
    echo ">> PG matchreports"
    pgq -c "\\copy matchreports TO STDOUT CSV" | gzip >"$dir/matchreports.csv.gz"

    {
        echo "{\"exported_at\": \"$(date -u +%FT%TZ)\","
        echo " \"matches_in_mart\": $(chq 'SELECT count(DISTINCT match_id) FROM manta.MatchTimelineFeatures FINAL'),"
        echo " \"collected\": $(pgq -t -A -c 'SELECT count(*) FROM collectedmatches'),"
        echo " \"reports\": $(pgq -t -A -c 'SELECT count(*) FROM matchreports')}"
    } >"$dir/meta.json"

    tar -cf "$out" -C "$dir" .
    echo
    echo "готово: $out ($(du -h "$out" | cut -f1))"
    cat "$dir/meta.json"
}

import_dataset() {
    local in="${1:?путь к архиву}"
    local dir; dir=$(mktemp -d)
    trap "rm -rf '$dir'" EXIT
    tar -xf "$in" -C "$dir"
    echo ">> архив: $(cat "$dir/meta.json" 2>/dev/null || echo 'без meta.json')"

    for t in "${REPLACING_TABLES[@]}"; do
        [ -f "$dir/$t.native.gz" ] || continue
        echo ">> CH $t (ReplacingMergeTree — вставка как есть)"
        gunzip -c "$dir/$t.native.gz" |
            docker exec -i "$CH" clickhouse-client --user "$CH_USER" --password "$CH_PASS" \
                -q "INSERT INTO manta.$t FORMAT Native"
    done

    for t in "${RAW_TABLES[@]}"; do
        [ -f "$dir/$t.native.gz" ] || continue
        echo ">> CH $t (MergeTree — только новые match_id через staging)"
        chq "DROP TABLE IF EXISTS manta.${t}_import"
        chq "CREATE TABLE manta.${t}_import AS manta.$t"
        gunzip -c "$dir/$t.native.gz" |
            docker exec -i "$CH" clickhouse-client --user "$CH_USER" --password "$CH_PASS" \
                -q "INSERT INTO manta.${t}_import FORMAT Native"
        chq "INSERT INTO manta.$t SELECT * FROM manta.${t}_import
             WHERE match_id NOT IN (SELECT DISTINCT match_id FROM manta.$t)"
        chq "DROP TABLE manta.${t}_import"
    done

    # COPY FROM STDIN: SQL и CSV-данные идут одним потоком (как pg_dump),
    # конец данных — строка «\.».
    echo ">> PG collectedmatches (ON CONFLICT DO NOTHING)"
    {
        echo "CREATE TEMP TABLE cm_import (LIKE collectedmatches INCLUDING ALL);"
        echo "COPY cm_import FROM STDIN CSV;"
        gunzip -c "$dir/collectedmatches.csv.gz"
        echo "\\."
        echo "INSERT INTO collectedmatches SELECT * FROM cm_import
                  ON CONFLICT (match_id) DO NOTHING;"
    } | pgq

    echo ">> PG matchreports (при конфликте побеждает свежий generated_at)"
    {
        echo "CREATE TEMP TABLE mr_import (LIKE matchreports INCLUDING ALL);"
        echo "COPY mr_import FROM STDIN CSV;"
        gunzip -c "$dir/matchreports.csv.gz"
        echo "\\."
        echo "INSERT INTO matchreports SELECT * FROM mr_import
                  ON CONFLICT (match_id) DO UPDATE SET
                      analysis = EXCLUDED.analysis,
                      timeline = EXCLUDED.timeline,
                      model_version = EXCLUDED.model_version,
                      feature_version = EXCLUDED.feature_version,
                      generated_at = EXCLUDED.generated_at
                  WHERE EXCLUDED.generated_at > matchreports.generated_at;"
    } | pgq

    echo
    echo ">> итог"
    echo "   матчей в витрине: $(chq 'SELECT count(DISTINCT match_id) FROM manta.MatchTimelineFeatures FINAL')"
    echo "   collectedmatches: $(pgq -t -A -c 'SELECT count(*) FROM collectedmatches')"
    echo "   matchreports:     $(pgq -t -A -c 'SELECT count(*) FROM matchreports')"
}

case "${1:-}" in
    export) shift; export_dataset "$@";;
    import) shift; import_dataset "$@";;
    *) usage;;
esac
