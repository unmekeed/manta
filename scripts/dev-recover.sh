#!/usr/bin/env bash
# Восстановление dev-стека после перезапуска среды (Гл. 10.4: пайплайн должен
# переживать эфемерность контейнера разработки).
#
# Среда разработки эфемерна: при простое её отзывают, погибают dockerd и все
# фоновые процессы (данные в docker volumes и /tmp при этом сохраняются).
# Скрипт идемпотентен — безопасно запускать и на живом стеке: каждый шаг
# сначала проверяет, не выполнен ли он уже.
#
#   ./scripts/dev-recover.sh            # поднять всё
#   MANTA_TRAIN_ENV=~/manta-train.env ./scripts/dev-recover.sh
#
# Секреты (Telegram и пр.) читаются из env-файла MANTA_TRAIN_ENV — он вне
# репозитория и в git не попадает.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
COMPOSE="docker compose -f deployments/docker-compose.yml"
TRAIN_ENV="${MANTA_TRAIN_ENV:-}"
LOG_DIR="${MANTA_LOG_DIR:-/tmp}"

say()  { printf '>> %s\n' "$*"; }
skip() { printf '   %s — уже работает, пропуск\n' "$*"; }

# Секреты (Telegram, OPENDOTA_API_KEY и пр.) — общий env-файл вне git,
# доступен всем шагам ниже (не только auto-train, как раньше).
if [ -n "$TRAIN_ENV" ] && [ -f "$TRAIN_ENV" ]; then
    set -a; . "$TRAIN_ENV"; set +a
else
    echo "   ВНИМАНИЕ: MANTA_TRAIN_ENV не задан/не найден — Telegram и OPENDOTA_API_KEY выключены" >&2
fi

# 1. dockerd -------------------------------------------------------------------
if docker info >/dev/null 2>&1; then
    skip "dockerd"
else
    say "запускаю dockerd"
    (sudo dockerd >"$LOG_DIR/dockerd.log" 2>&1 &)
    for _ in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    docker info >/dev/null 2>&1 || { echo "dockerd не поднялся, см. $LOG_DIR/dockerd.log" >&2; exit 1; }
fi

# 2. Инфраструктура (данные — в volumes, переживают перезапуск) ----------------
say "поднимаю инфраструктуру (postgres, clickhouse, kafka, minio, redis)"
$COMPOSE up -d postgres clickhouse kafka minio redis >/dev/null

say "жду ClickHouse"
for _ in $(seq 1 60); do
    [ "$(curl -s http://localhost:8123/ping 2>/dev/null)" = "Ok." ] && break
    sleep 2
done
[ "$(curl -s http://localhost:8123/ping)" = "Ok." ] || { echo "ClickHouse не отвечает" >&2; exit 1; }

say "жду Kafka"
for _ in $(seq 1 60); do
    docker exec manta-kafka-1 kafka-topics.sh --bootstrap-server localhost:9092 --list >/dev/null 2>&1 && break
    sleep 2
done

# 3. Бинарники (пересборка только если отсутствуют) ----------------------------
if [ ! -x apps/replay-parser/build/demoinfo ]; then
    say "собираю C++ ядро парсера"
    cmake -B apps/replay-parser/build -S apps/replay-parser -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build apps/replay-parser/build -j"$(nproc)" >/dev/null
fi
if [ ! -x /tmp/parser-svc ]; then
    say "собираю parser-svc"
    (cd apps/replay-parser/svc && go build -o /tmp/parser-svc ./cmd/parser-svc)
fi

# 4. Хост-сервисы конвейера ----------------------------------------------------
if ! pgrep -f "^/tmp/parser-svc" >/dev/null; then
    say "запускаю parser-svc (лог: $LOG_DIR/parser-svc.log)"
    DEMOINFO_PATH="$ROOT/apps/replay-parser/build/demoinfo" WORK_DIR=/tmp \
        PURGE_PARSED_REPLAYS=true \
        nohup /tmp/parser-svc >"$LOG_DIR/parser-svc.log" 2>&1 &
else
    skip "parser-svc"
fi

if ! pgrep -f "python3 -u -m serve_features" >/dev/null; then
    say "запускаю feature-store (gRPC :50055, лог: $LOG_DIR/feature-store.log)"
    (cd apps/feature-store && PYTHONPATH=src \
        nohup python3 -u -m serve_features >"$LOG_DIR/feature-store.log" 2>&1 &)
else
    skip "feature-store"
fi

if ! pgrep -f "python3 -u -m extractor" >/dev/null; then
    say "запускаю feature-extractor (лог: $LOG_DIR/extractor.log)"
    (cd apps/feature-extractor && PYTHONPATH=src \
        FEATURE_STORE_ADDR="${FEATURE_STORE_ADDR:-localhost:50055}" \
        nohup python3 -u -m extractor >"$LOG_DIR/extractor.log" 2>&1 &)
else
    skip "feature-extractor"
fi

# Бюджет анонимного тарифа OpenDota (без OPENDOTA_API_KEY): ~50k
# вызовов/месяц ≈ 1660/сутки на IP, burst-потолок 60/мин. Дефолты ниже
# суммарно дают ~1100-1200 вызовов/сутки — сбор идёт круглосуточно,
# а не сгорает за 3-4 часа (runbook «витрина не растёт»). С ключом
# лимиты можно вернуть агрессивные через env.
if ! pgrep -f "collector --source opendota-public" >/dev/null; then
    say "запускаю data-collector (лог: $LOG_DIR/collector.log)"
    (cd apps/data-collector && OPENDOTA_LIMIT="${OPENDOTA_LIMIT:-1}" PYTHONPATH=src \
        nohup python3 -u -m collector --source opendota-public \
            --interval "${PUBLIC_REPLAY_INTERVAL:-3600}" \
            >"$LOG_DIR/collector.log" 2>&1 &)
else
    skip "data-collector"
fi

if ! pgrep -f "collector --source opendota-timeline --interval" >/dev/null; then
    say "запускаю timeline-collector (лог: $LOG_DIR/timeline.log)"
    (cd apps/data-collector && TIMELINE_LIMIT="${TIMELINE_LIMIT:-10}" PYTHONPATH=src \
        nohup python3 -u -m collector --source opendota-timeline \
            --interval "${TIMELINE_INTERVAL:-1800}" \
            >"$LOG_DIR/timeline.log" 2>&1 &)
else
    skip "timeline-collector"
fi

if ! pgrep -f "collector --source opendota-timeline-pro" >/dev/null; then
    say "запускаю pro-timeline-collector (лог: $LOG_DIR/timeline-pro.log)"
    (cd apps/data-collector && TIMELINE_LIMIT="${PRO_TIMELINE_LIMIT:-5}" PYTHONPATH=src \
        nohup python3 -u -m collector --source opendota-timeline-pro \
            --interval "${PRO_TIMELINE_INTERVAL:-3600}" \
            >"$LOG_DIR/timeline-pro.log" 2>&1 &)
else
    skip "pro-timeline-collector"
fi

if ! pgrep -f "collector --source opendota --interval" >/dev/null; then
    say "запускаю pro-replay-collector (лог: $LOG_DIR/pro-collector.log)"
    (cd apps/data-collector && OPENDOTA_LIMIT=1 METRICS_PORT=9109 PYTHONPATH=src \
        nohup python3 -u -m collector --source opendota \
            --interval "${PRO_REPLAY_INTERVAL:-3600}" \
            >"$LOG_DIR/pro-collector.log" 2>&1 &)
else
    skip "pro-replay-collector"
fi

if ! pgrep -f "python3 -u -m app" >/dev/null; then
    say "запускаю ml-service (gRPC, лог: $LOG_DIR/ml-serve.log)"
    (cd apps/ml-service && PYTHONPATH=src \
        MODEL_PATH="${MODEL_PATH:-registry://win_probability/production}" \
        nohup python3 -u -m app >"$LOG_DIR/ml-serve.log" 2>&1 &)
else
    skip "ml-service"
fi

if ! pgrep -f "python3 -u -m serve" >/dev/null; then
    say "запускаю similarity (gRPC :50052, лог: $LOG_DIR/similarity.log)"
    (cd apps/similarity && PYTHONPATH=src \
        nohup python3 -u -m serve >"$LOG_DIR/similarity.log" 2>&1 &)
else
    skip "similarity"
fi

if ! pgrep -f "python3 -u -m serve_draft" >/dev/null; then
    say "запускаю draft (gRPC :50053, лог: $LOG_DIR/draft.log)"
    (cd apps/draft && PYTHONPATH=src \
        nohup python3 -u -m serve_draft >"$LOG_DIR/draft.log" 2>&1 &)
else
    skip "draft"
fi

if ! pgrep -f "python3 -u -m serve_coach" >/dev/null; then
    say "запускаю coach (gRPC :50054, лог: $LOG_DIR/coach.log)"
    (cd apps/coach && PYTHONPATH=src \
        nohup python3 -u -m serve_coach >"$LOG_DIR/coach.log" 2>&1 &)
else
    skip "coach"
fi

if ! pgrep -f "python3 -u -m reportgen" >/dev/null; then
    say "запускаю report-generator (лог: $LOG_DIR/report-gen.log)"
    (cd apps/report-generator && PYTHONPATH=src \
        nohup python3 -u -m reportgen >"$LOG_DIR/report-gen.log" 2>&1 &)
else
    skip "report-generator"
fi

# 5. Авто-обучение (+ Telegram-уведомления из env-файла) -----------------------
if ! pgrep -f "python3 -u -m training.auto" >/dev/null; then
    say "запускаю auto-train (лог: $LOG_DIR/wp-auto.log)"
    (cd apps/ml-service && PYTHONPATH=src \
        nohup python3 -u -m training.auto >"$LOG_DIR/wp-auto.log" 2>&1 &)
else
    skip "auto-train"
fi

# 6. Итог ----------------------------------------------------------------------
sleep 3
echo
say "статус"
printf '   %-18s %s\n' clickhouse "$(curl -s http://localhost:8123/ping)"
check() { printf '   %-18s %s\n' "$1" "$(pgrep -f "$2" >/dev/null && echo OK || echo DOWN)"; }
check parser-svc "^/tmp/parser-svc"
check feature-extractor "python3 -u -m extractor"
check data-collector "collector --source opendota-public"
check timeline-coll. "collector --source opendota-timeline --interval"
check pro-timeline "collector --source opendota-timeline-pro"
check pro-replay "collector --source opendota --interval"
check ml-service "python3 -u -m app"
check similarity "python3 -u -m serve"
check draft "python3 -u -m serve_draft"
check coach "python3 -u -m serve_coach"
check feature-store "python3 -u -m serve_features"
check report-generator "python3 -u -m reportgen"
check auto-train "python3 -u -m training.auto"
matches=$(echo "SELECT count(DISTINCT match_id) FROM manta.MatchTimelineFeatures FINAL" |
    curl -s "http://localhost:8123/?database=manta" \
        -H "X-ClickHouse-User: dota" -H "X-ClickHouse-Key: dota_dev_password" --data-binary @- || echo '?')
printf '   %-18s %s\n' "матчей в витрине" "$matches"
