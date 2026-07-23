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
# Логи — вне /tmp (спринт 49, инцидент №8: /tmp гибнет при рестарте WSL,
# истории для диагностики не остаётся).
LOG_DIR="${MANTA_LOG_DIR:-$HOME/manta-logs}"
mkdir -p "$LOG_DIR"

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

# 2b. Гарантии данных (спринт 49): топики и миграции — идемпотентно на
# КАЖДОМ запуске. Инцидент №6: volume Kafka пересоздался, топики пропали,
# реплейный путь молча стоял неделями; инцидент №7: непрогнанная миграция
# после git pull. Оба класса проблем recover теперь закрывает сам.
say "топики Kafka (create --if-not-exists)"
./infra/kafka/create-topics.sh >/dev/null
say "миграции Postgres (только новые, журнал SchemaMigrations)"
./scripts/pg-migrate.sh | sed 's/^/   /'
say "миграции ClickHouse (только новые, журнал SchemaMigrations)"
./scripts/ch-migrate.sh | sed 's/^/   /'

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

# 3b. Python-зависимости (спринт 53: чистая машина — recover запускал
# сервисы без единого pip install, все падали ModuleNotFoundError молча
# в лог). Штамп по хэшу requirements.txt — pip install при уже
# удовлетворённых зависимостях быстрый, но не бесплатный на 8 сервисах.
say "python-зависимости сервисов (пропуск уже установленных)"
command -v pip3 >/dev/null || sudo apt-get install -y python3-pip
STAMP_DIR="$LOG_DIR/.pip-stamps"
mkdir -p "$STAMP_DIR"
for req in apps/*/requirements.txt; do
    svc=$(basename "$(dirname "$req")")
    stamp="$STAMP_DIR/$svc.sha256"
    hash=$(sha256sum "$req" | cut -d' ' -f1)
    if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$hash" ]; then
        continue
    fi
    say "  pip install: $svc"
    pip3 install --break-system-packages -q -r "$req" && echo "$hash" > "$stamp"
done

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

# 5b. UI-контур: gateway, frontend, дашборд (спринт 51: recover поднимает
# ВСЁ одной командой, руками ничего не запускается) ----------------------------
if [ ! -x /tmp/api-gateway ]; then
    say "собираю api-gateway"
    (cd apps/api-gateway && go build -o /tmp/api-gateway ./cmd/server)
fi
if ! pgrep -f "^/tmp/api-gateway" >/dev/null; then
    say "запускаю api-gateway (:8080, лог: $LOG_DIR/gateway.log)"
    # HEROES_PATH: дефолт бинарника (../../libs/…) рассчитан на запуск из
    # каталога gateway — из корня словарь героев не нашёлся бы (503 /heroes).
    HEROES_PATH="${HEROES_PATH:-$ROOT/libs/data/heroes.json}" \
        nohup /tmp/api-gateway >"$LOG_DIR/gateway.log" 2>&1 &
else
    skip "api-gateway"
fi

if ! pgrep -f "vite --host" >/dev/null; then
    say "запускаю frontend (vite, :5173, лог: $LOG_DIR/frontend.log)"
    (cd apps/frontend && { [ -d node_modules ] || npm ci --silent; } && \
        nohup npm run dev -- --host 0.0.0.0 --port 5173 \
            >"$LOG_DIR/frontend.log" 2>&1 &)
else
    skip "frontend"
fi

if ! pgrep -f "scripts/dashboard.py" >/dev/null; then
    say "запускаю дашборд (:9107, лог: $LOG_DIR/dashboard.log)"
    nohup python3 scripts/dashboard.py >"$LOG_DIR/dashboard.log" 2>&1 &
else
    skip "дашборд"
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
check api-gateway "^/tmp/api-gateway"
check frontend "vite --host"
check dashboard "scripts/dashboard.py"
matches=$(echo "SELECT count(DISTINCT match_id) FROM manta.MatchTimelineFeatures FINAL" |
    curl -s "http://localhost:8123/?database=manta" \
        -H "X-ClickHouse-User: dota" -H "X-ClickHouse-Key: dota_dev_password" --data-binary @- || echo '?')
printf '   %-18s %s\n' "матчей в витрине" "$matches"

echo
say "адреса"
printf '   %-18s %s\n' "веб-интерфейс" "http://localhost:5173"
printf '   %-18s %s\n' "дашборд" "http://localhost:9107"
printf '   %-18s %s\n' "REST API" "http://localhost:8080/healthz"

# 7. Doctor: здоровье по ДАННЫМ, а не по pgrep (свежие сервисы ещё не успели
# ничего записать — поэтому не роняем recover, только показываем).
echo
say "doctor (health-check по данным; отдельно: make doctor)"
./scripts/doctor.sh || true
