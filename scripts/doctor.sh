#!/usr/bin/env bash
# make doctor — health-check конвейера ПО ДАННЫМ, а не по процессам
# (спринт 49; мета-урок HANDOFF: «процесс жив» != «конвейер работает» —
# реплейный путь стоял неделями при зелёном pgrep).
#
# Проверки: контейнеры, Kafka-топики и консьюмер-группы, свежесть данных
# (ReplayEvents/PositionSnapshots/витрина), квота OpenDota, часы хоста vs
# ClickHouse, применённость миграций. Выход: 0 — нет FAIL, иначе 1.
set -uo pipefail
cd "$(dirname "$0")/.."

CH_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
CH_DB="${CLICKHOUSE_DB:-manta}"
CH_AUTH=(-H "X-ClickHouse-User: ${CLICKHOUSE_USER:-dota}"
         -H "X-ClickHouse-Key: ${CLICKHOUSE_PASSWORD:-dota_dev_password}")
KAFKA_BIN="docker exec manta-kafka-1 /opt/kafka/bin"
REPLAY_STALL_H="${REPLAY_STALL_ALERT_H:-6}"
DATASET_STALL_H="${DATASET_STALL_ALERT_H:-12}"

fails=0; warns=0
ok()   { printf '   \033[32m OK \033[0m %s\n' "$*"; }
warn() { printf '   \033[33mWARN\033[0m %s\n' "$*"; warns=$((warns + 1)); }
fail() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; fails=$((fails + 1)); }
ch()   { echo "$1" | curl -s --max-time 10 "$CH_URL/?database=$CH_DB" \
             "${CH_AUTH[@]}" --data-binary @-; }

echo "== Контейнеры инфраструктуры"
for c in postgres clickhouse kafka minio redis; do
    st=$(docker inspect -f '{{.State.Health.Status}}' "manta-$c-1" \
             2>/dev/null | tr -d '[:space:]')
    [ -z "$st" ] && st=missing
    case "$st" in
        healthy) ok "$c" ;;
        missing) fail "$c отсутствует — make recover" ;;
        *)       fail "$c: $st" ;;
    esac
done

echo "== Kafka: топики (инцидент №6 — продюсер теряет сообщения МОЛЧА)"
topics=$($KAFKA_BIN/kafka-topics.sh --bootstrap-server localhost:9092 \
             --list 2>/dev/null)
for t in match.downloaded replay.parsed features.calculated \
         prediction.completed report.generated meta.updated dlq.parser; do
    if grep -qx "$t" <<<"$topics"; then ok "топик $t"
    else fail "топик $t ОТСУТСТВУЕТ — make topics + перезапуск parser-svc/extractor"
    fi
done

echo "== Kafka: консьюмер-группы (group id парсера — replay-parser, НЕ parser-svc)"
for g in replay-parser feature-extractor; do
    desc=$($KAFKA_BIN/kafka-consumer-groups.sh --bootstrap-server \
               localhost:9092 --describe --group "$g" 2>/dev/null)
    if [ -z "$desc" ]; then
        fail "группа $g не существует — консьюмер ни разу не подключался"
    else
        lag=$(awk 'NR > 1 && $6 ~ /^[0-9]+$/ {s += $6} END {print s + 0}' \
                  <<<"$desc")
        if [ "$lag" -gt 1000 ]; then warn "группа $g: лаг $lag и растёт?"
        else ok "группа $g (лаг $lag)"
        fi
    fi
done

echo "== Свежесть данных (главная проверка: конвейер жив, если данные свежие)"
now=$(date -u +%s)
re_ts=$(ch "SELECT toUnixTimestamp(max(ingested_at)) FROM ReplayEvents")
if ! [[ "$re_ts" =~ ^[0-9]+$ ]]; then
    fail "ClickHouse не ответил (ReplayEvents): '$re_ts'"
elif [ "$re_ts" = "0" ]; then
    fail "ReplayEvents ПУСТА — реплейный путь никогда не писал (топики? parser-svc?)"
else
    age_h=$(( (now - re_ts) / 3600 ))
    if [ "$age_h" -ge "$REPLAY_STALL_H" ]; then
        fail "ReplayEvents: последняя вставка ${age_h}ч назад (порог ${REPLAY_STALL_H}ч)"
    else
        ok "ReplayEvents: свежесть ${age_h}ч"
    fi
fi
ps_ts=$(ch "SELECT toUnixTimestamp(max(modification_time)) FROM system.parts
            WHERE database = '$CH_DB' AND table = 'PositionSnapshots' AND active")
if [[ "$ps_ts" =~ ^[0-9]+$ ]] && [ "$ps_ts" != "0" ]; then
    age_h=$(( (now - ps_ts) / 3600 ))
    if [ "$age_h" -ge "$REPLAY_STALL_H" ]; then
        warn "PositionSnapshots: последняя запись ${age_h}ч назад"
    else
        ok "PositionSnapshots: свежесть ${age_h}ч"
    fi
else
    warn "PositionSnapshots: нет активных парт (пустая таблица?)"
fi
mt_ts=$(ch "SELECT toUnixTimestamp(max(computed_at)) FROM MatchTimelineFeatures")
n_matches=$(ch "SELECT count(DISTINCT match_id) FROM MatchTimelineFeatures FINAL")
if [[ "$mt_ts" =~ ^[0-9]+$ ]] && [ "$mt_ts" != "0" ]; then
    age_h=$(( (now - mt_ts) / 3600 ))
    if [ "$age_h" -ge "$DATASET_STALL_H" ]; then
        fail "витрина не растёт ${age_h}ч (${n_matches} матчей) — квота/коллекторы?"
    else
        ok "витрина: свежесть ${age_h}ч, матчей ${n_matches}"
    fi
else
    fail "витрина MatchTimelineFeatures пуста или недоступна"
fi

echo "== Квота OpenDota (по IP; сброс 00:00 UTC)"
q=$(curl -sI --max-time 10 https://api.opendota.com/api/health |
        tr -d '\r' | awk -F': ' 'tolower($1) == "x-rate-limit-remaining-day" {print $2}')
if [ -z "$q" ]; then warn "OpenDota недоступен — квоту не узнать"
elif [ "${q#-}" != "$q" ] || [ "$q" -lt 100 ]; then
    warn "квота на исходе: remaining-day=$q (коллекторы уснут до 00:00 UTC)"
else ok "remaining-day=$q"
fi

echo "== Часы хоста vs ClickHouse (WSL2 дрейфует после сна)"
ch_now=$(ch "SELECT toUnixTimestamp(now())")
if [[ "$ch_now" =~ ^[0-9]+$ ]]; then
    drift=$(( now > ch_now ? now - ch_now : ch_now - now ))
    if [ "$drift" -gt 60 ]; then
        fail "расхождение ${drift}с — из PowerShell: wsl --shutdown"
    else
        ok "расхождение ${drift}с"
    fi
else
    warn "ClickHouse не ответил на запрос времени"
fi

echo "== Миграции"
export PGPASSWORD="${PGPASSWORD:-dota_dev_password}"
applied=$(psql -h "${POSTGRES_HOST:-localhost}" -U "${POSTGRES_USER:-dota}" \
              -d "${POSTGRES_DB:-manta}" -qtA \
              -c "SELECT filename FROM SchemaMigrations" 2>/dev/null)
if [ -z "$applied" ]; then
    fail "журнал SchemaMigrations пуст/недоступен — make migrate"
else
    for f in infra/migrations/postgres/*.sql; do
        b=$(basename "$f")
        grep -qx "$b" <<<"$applied" || fail "PG-миграция $b не применена — make migrate"
    done
    ok "PG-миграции: журнал полон"
fi
# CH-миграции журнала не ведут (все идемпотентны) — проверяем маркер
# ПОСЛЕДНЕЙ (009: networth_total). При добавлении миграции обновить маркер.
sentinel=$(ch "SELECT count() FROM system.columns WHERE database = '$CH_DB'
               AND table = 'MatchTimelineFeatures' AND name = 'networth_total'")
if [ "$sentinel" = "1" ]; then ok "CH-миграции: маркер 009 на месте"
else fail "CH-миграции отстают (нет networth_total) — make migrate"
fi

echo
if [ "$fails" -eq 0 ]; then
    printf '\033[32m>> ЗДОРОВ\033[0m (warn: %d)\n' "$warns"
    exit 0
fi
printf '\033[31m>> ПРОБЛЕМ: %d\033[0m (warn: %d) — лечение: docs/runbooks.md\n' \
    "$fails" "$warns"
exit 1
