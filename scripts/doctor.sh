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

# Тот же env-файл, что читает dev-recover.sh. Без него doctor не знает,
# какие опциональные коллекторы вообще должны работать на этой машине
# (STRATZ поднимается только при токене) — и молча их не проверял.
# Отсутствие файла не ошибка: проверки по данным работают и без него.
TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
if [ -f "$TRAIN_ENV" ]; then
    set -a; . "$TRAIN_ENV"; set +a
fi

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

# Деньги за месяц. Платный тариф OpenDota считает КАЛЕНДАРНЫЙ месяц
# ($0.01 за 100 вызовов), а суточная квота про месяц не знает ничего:
# 15 000 в сутки — это $46,5 в месяце из 31 дня, и без этой строки о
# перерасходе узнают из счёта, когда менять что-либо поздно.
#
# Считается только при заданном потолке: на бесплатном тарифе вызовы
# денег не стоят, и строка про доллары была бы выдумкой — той самой, от
# которой перестают верить остальным строкам.
if [ "${OPENDOTA_MONTHLY_LIMIT:-0}" -gt 0 ] 2>/dev/null; then
    # api = 'opendota': соли GC живут в той же таблице под своим именем
    # и денег не стоят. Сложи их сюда — и доктор объявил бы перерасход
    # там, где не потрачено ни цента.
    # `docker exec` и `psql` — В ОДНОЙ строке: проверка «psql только через
    # контейнер» смотрит построчно, и перенос читался бы как хостовый
    # вызов с дев-паролем. Поймано тестом.
    calls=$(docker exec "${PG_CONTAINER:-manta-postgres-1}" psql -U "${POSTGRES_USER:-dota}" -d "${POSTGRES_DB:-manta}" -tAc \
        "SELECT coalesce(sum(calls), 0) FROM ApiBudget
          WHERE api = 'opendota'
            AND day >= date_trunc('month', (NOW() AT TIME ZONE 'UTC'))::date" \
        2>/dev/null | tr -d ' ')
    if [ -z "$calls" ]; then
        warn "месячный расход OpenDota неизвестен — база не ответила"
    else
        cost=$(awk -v c="$calls" -v r="${OPENDOTA_COST_PER_CALL:-0.0001}" \
               'BEGIN{printf "%.2f", c*r}')
        pct=$(awk -v c="$calls" -v l="$OPENDOTA_MONTHLY_LIMIT" \
              'BEGIN{printf "%d", c*100/l}')
        line="OpenDota за месяц: \$$cost ($pct% потолка)"
        if [ "$pct" -ge "${OPENDOTA_ALERT_PCT:-80}" ]; then
            warn "$line"
        else
            ok "$line"
        fi
    fi
fi

# Вердикт этого скрипта — по ДАННЫМ (мета-урок HANDOFF), и это не
# меняется: живой pgrep ничего не доказывает. Но обратное показание
# полезно как ПОДСКАЗКА: когда витрина стоит, а квота цела, ответ на
# вопрос «квота или коллекторы?» — ровно в этом списке. На вердикт и код
# возврата секция не влияет.
echo "== Сервисы (подсказка к диагнозу; вердикт — по данным)"
# Сервис жив, если работает ЛИБО его контейнер, ЛИБО хостовый процесс.
#
# Топологии две: дома сервисы поднимает dev-recover.sh процессами, на VPS
# они живут в Docker. До спринта 163 проверка знала только про первую и на
# VPS сообщала «11 процессов не запущено» при двадцати работающих
# контейнерах. Предупреждение, неустранимое на этой машине в принципе, —
# и оно уходило в Telegram вместе с ежедневным вердиктом сторожа.
#
# Топология здесь намеренно НЕ вычисляется. Определять «где мы» значит
# завести ещё одно предположение, которое однажды разойдётся с
# действительностью; спросить оба места дешевле и не ошибается.
down=0; missing=""
svc() {                       # svc <имя> <контейнер> <шаблон процесса>
    if docker inspect -f '{{.State.Running}}' "$2" 2>/dev/null | grep -qx true; then
        printf '        %-18s %s\n' "$1" "up (контейнер)"
    elif pgrep -f "$3" >/dev/null; then
        printf '        %-18s %s\n' "$1" "up (процесс)"
    else
        printf '   \033[33m  ?\033[0m %-18s %s\n' "$1" "DOWN"
        down=$((down + 1)); missing="$missing $1"
    fi
}
svc data-collector     manta-data-collector-1         "collector --source opendota-public"
svc timeline-coll.     manta-timeline-collector-1     "collector --source opendota-timeline --interval"
svc pro-timeline       manta-pro-timeline-collector-1 "collector --source opendota-timeline-pro"
svc league-coll.       manta-league-collector-1       "collector --source opendota-league"
svc pro-replay         manta-pro-replay-collector-1   "collector --source opendota --interval"
# STRATZ опционален: без токена этих коллекторов быть и не должно, и
# отмечать их DOWN было бы ложной тревогой.
if [ -n "${STRATZ_API_TOKEN:-}" ]; then
    svc stratz-coll.   manta-stratz-collector-1       "collector --source stratz-timeline --interval"
    # stratz-timeline-pro намеренно не запускается со спринта 93 — весь
    # про-поток идёт через opendota-timeline-pro. Проверять его здесь
    # значило бы каждый раз поднимать ложную тревогу.
fi
svc parser-svc         manta-parser-svc-1             "^/tmp/parser-svc"
svc feature-extractor  manta-feature-extractor-1      "python3 -u -m extractor"
svc ml-service         manta-ml-service-1             "python3 -u -m app"
svc report-generator   manta-report-generator-1       "python3 -u -m reportgen"
svc auto-train         manta-ml-autotrain-1           "python3 -u -m training.auto"
if [ "$down" -gt 0 ]; then
    warn "не работают:$missing — если данные стоят, начинать отсюда"
else
    ok "все ключевые сервисы работают"
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
# Postgres спрашивается ЧЕРЕЗ КОНТЕЙНЕР, как и всё остальное в проекте.
#
# Здесь стоял хостовый psql с дев-паролем по умолчанию — на VPS такого
# клиента нет и быть не должно (спринты 143 и 152: инструмент берём
# оттуда, где он уже есть). Клиента нет → запрос падает → `2>/dev/null`
# съедает «command not found» → печатается «журнал пуст». Диагноз уводил
# чинить миграции, которые в полном порядке, и делал вердикт doctor
# вечным FAIL. В двадцати строках ниже проверка ClickHouse всё это время
# ходила правильно — через контейнер.
#
# «Не смог спросить» и «журнал пуст» — РАЗНЫЕ беды, и различать их
# обязательно: первое про инструмент, второе про данные.
# Без `-i`: доктора запускают и из скриптов, и из тестов, где на входе
# висит труба, которую никто не закроет. `docker exec -i` тянул бы из неё
# вечно — проверка здоровья, которая сама зависает, хуже отсутствующей.
# Данные внутрь мы не передаём, весь запрос идёт аргументом `-c`.
PG_CONTAINER="${PG_CONTAINER:-manta-postgres-1}"
applied=""
pg_reachable=0
if docker exec "$PG_CONTAINER" psql -U "${POSTGRES_USER:-dota}" \
        -d "${POSTGRES_DB:-manta}" -qtA -c "SELECT 1" >/dev/null 2>&1; then
    pg_reachable=1
    applied=$(docker exec "$PG_CONTAINER" psql -U "${POSTGRES_USER:-dota}" \
                  -d "${POSTGRES_DB:-manta}" -qtA \
                  -c "SELECT filename FROM SchemaMigrations" 2>/dev/null)
fi
if [ "$pg_reachable" = "0" ]; then
    warn "не смог спросить Postgres ($PG_CONTAINER) — журнал миграций не проверен"
elif [ -z "$applied" ]; then
    fail "журнал SchemaMigrations пуст — make migrate"
else
    for f in infra/migrations/postgres/*.sql; do
        b=$(basename "$f")
        grep -qx "$b" <<<"$applied" || fail "PG-миграция $b не применена — make migrate"
    done
    ok "PG-миграции: журнал полон"
fi
# CH-миграции с спринта 54 ведут журнал manta.SchemaMigrations — сверяем
# его с каталогом файлов, как для PG. Раньше здесь был захардкоженный
# маркер последней миграции: его требовалось обновлять руками, и он,
# конечно, отстал (остался на 010, когда появились 011 и 012).
ch_applied=$(ch "SELECT filename FROM SchemaMigrations")
if [ -z "$ch_applied" ]; then
    fail "журнал CH SchemaMigrations пуст/недоступен — make migrate"
else
    ch_missing=0
    for f in infra/migrations/clickhouse/*.sql; do
        b=$(basename "$f")
        grep -qx "$b" <<<"$ch_applied" || {
            fail "CH-миграция $b не применена — make migrate"; ch_missing=1; }
    done
    [ "$ch_missing" -eq 0 ] && ok "CH-миграции: журнал полон"
fi

echo
if [ "$fails" -eq 0 ]; then
    printf '\033[32m>> ЗДОРОВ\033[0m (warn: %d)\n' "$warns"
    exit 0
fi
printf '\033[31m>> ПРОБЛЕМ: %d\033[0m (warn: %d) — лечение: docs/runbooks.md\n' \
    "$fails" "$warns"
exit 1
