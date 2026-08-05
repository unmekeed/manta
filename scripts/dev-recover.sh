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
# Дефолт — тот же, что у scripts/manta (спринт 107). Без него `make
# recover` работал БЕЗ токенов: Makefile подставляет MANTA_TRAIN_ENV=
# пустым (переменная в нём не определена), и env-файл не читался вовсе.
# До спринта 106 это сходило с рук — recover пропускал живые сервисы, а
# поднимал их `manta up`, который дефолт подставляет. Теперь recover
# перезапускает устаревшие процессы, то есть отобрал бы у них ключ
# OpenDota, токен STRATZ и Telegram, а выглядело бы это как обычный
# рестарт. Пустое значение `:-` считает отсутствующим — поэтому явная
# передача пустышки из Makefile тоже лечится здесь.
TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
# Логи — вне /tmp (спринт 49, инцидент №8: /tmp гибнет при рестарте WSL,
# истории для диагностики не остаётся).
LOG_DIR="${MANTA_LOG_DIR:-$HOME/manta-logs}"
mkdir -p "$LOG_DIR"

# ТАБЛИЦА СЕРВИСОВ — единственная на весь скрипт (спринт 115).
#
# Раньше их было две: список в цикле сверки свежести и вызовы `check` в
# статусе. 2026-08-05 они разошлись — `check stratz-coll.` стоит внутри
# `if [ -n "$STRATZ_API_TOKEN" ]`, поэтому не попал в список свежести, и
# stratz-коллектор сутки крутил старый код, пока остальные обновлялись.
# Внешне всё было исправно: статус OK, процесс жив, лог пишется.
#
# Ровно об этом предупреждал комментарий в спринте 106 — «карта, какой
# файл чей, молча устареет». Устарела не карта файлов, а карта сервисов,
# и за один спринт. Поэтому источник теперь один.
#
# Дашборда здесь нет НАМЕРЕННО: у него отдельный блок с защитой от
# самоубийства, когда recover запущен его же кнопкой.
SERVICES=(
    "parser-svc|^/tmp/parser-svc"
    "feature-extractor|python3 -u -m extractor"
    "data-collector|collector --source opendota-public"
    "timeline-coll.|collector --source opendota-timeline --interval"
    "pro-timeline|collector --source opendota-timeline-pro"
    "league-coll.|collector --source opendota-league"
    "pro-replay|collector --source opendota --interval"
    "stratz-coll.|collector --source stratz-timeline --interval"
    "ml-service|python3 -u -m app"
    "similarity|python3 -u -m serve$"
    "draft|python3 -u -m serve_draft"
    "coach|python3 -u -m serve_coach"
    "feature-store|python3 -u -m serve_features"
    "report-generator|python3 -u -m reportgen"
    "auto-train|python3 -u -m training.auto"
    "api-gateway|^/tmp/api-gateway"
    "frontend|vite --host"
)

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

# ВАЖНО: каждому сервису METRICS_PORT задаётся ЯВНО (инцидент 2026-07-27).
# В коде у каждого свой дефолт (9102-9114), столкнуться они не могли, но
# env-файл читается через `set -a`, и одна строка `export METRICS_PORT=9106`
# раздавалась ВСЕМ: первый стартовавший занимал порт, остальные умирали с
# OSError: Address already in use ещё до начала работы. Коллекторы стояли
# неделю из-за Prometheus-эндпоинта. Явное присваивание перекрывает
# унаследованную переменную и делает конфигурацию env-файла безопасной.
# 3.5. Свежесть кода (спринт 106) ----------------------------------------------
#
# recover запускает сервис, только если тот ещё не жив (`if ! pgrep`).
# Поэтому после `git pull` он честно докладывает «уже работает, пропуск»
# — и стек продолжает крутить код, загруженный ДО правки, а doctor при
# этом рапортует ЗДОРОВ. Ловушка описана в runbook 5 словами со спринта
# 101, но в коде была закрыта только для дашборда.
#
# 2026-08-05 это стоило двух ложных заходов подряд: спринт 104 добавил
# лигам лог причин отказа — лога не появилось; спринт 105 починил
# справочник предметов — имена не изменились. Обе правки были верны,
# просто стек их не видел.
#
# Решение: до старта сервисов убиваем те, что стартовали РАНЬШЕ последней
# правки исходников. Дальше обычные блоки `if ! pgrep` поднимают их сами,
# отдельной логики запуска не нужно.
#
# Отметка ОДНА на всё дерево, а не по сервису. Точная привязка «какой
# файл чей» — это карта, которая молча устареет при первом рефакторе, и
# тогда часть сервисов останется на старом коде, а выглядеть всё будет
# исправно. Лишний перезапуск стоит секунд; незамеченная старая версия —
# суток отладки.
newest_code=$(find apps libs scripts \
        \( -name node_modules -o -name __pycache__ -o -name .git \
           -o -name dist -o -name build -o -name models \) -prune -o \
        -type f \( -name '*.py' -o -name '*.go' -o -name '*.sh' \) \
        -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
newest_code=${newest_code%%.*}

# env-файл считается частью «кода» (спринт 109). Токены, лимиты циклов и
# номер шарда сервис читает ОДИН РАЗ при старте, поэтому правка
# manta-train.env без перезапуска не значит ничего — а выглядит так, будто
# настройка применена. Та же подмена «изменил» на «подействовало», что и с
# git pull до спринта 106.
if [ -f "$TRAIN_ENV" ]; then
    env_mtime=$(stat -c %Y "$TRAIN_ENV" 2>/dev/null || echo 0)
    [ "${env_mtime:-0}" -gt "${newest_code:-0}" ] && newest_code="$env_mtime"
fi

restart_if_stale() {   # имя  шаблон-pgrep
    local name="$1" pat="$2" pid age started
    [ -n "${newest_code:-}" ] || return 0
    pid=$(pgrep -f "$pat" | head -1 || true)
    [ -n "$pid" ] || return 0
    age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    # Процесс мог исчезнуть между pgrep и ps: пустой возраст значит «не
    # знаем», и тогда считаем код новее — перезапуск дешевле, чем ещё
    # сутки на устаревшей версии.
    started=$(( $(date +%s) - ${age:-0} ))
    [ "$newest_code" -gt "$started" ] || return 0
    say "$name старее своего кода — перезапускаю"
    pkill -f "$pat" 2>/dev/null || true
    sleep 1
}

# Дашборд здесь НЕ трогаем: у него отдельный блок ниже, с защитой от
# самоубийства, когда recover запущен его же кнопкой.
for svc in "${SERVICES[@]}"; do
    restart_if_stale "${svc%%|*}" "${svc#*|}"
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
    (cd apps/feature-store && METRICS_PORT=9114 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m serve_features >"$LOG_DIR/feature-store.log" 2>&1 &)
else
    skip "feature-store"
fi

if ! pgrep -f "python3 -u -m extractor" >/dev/null; then
    say "запускаю feature-extractor (лог: $LOG_DIR/extractor.log)"
    (cd apps/feature-extractor && METRICS_PORT=9102 PYTHONPATH=src:$ROOT/libs \
        FEATURE_STORE_ADDR="${FEATURE_STORE_ADDR:-localhost:50055}" \
        nohup python3 -u -m extractor >"$LOG_DIR/extractor.log" 2>&1 &)
else
    skip "feature-extractor"
fi

# Бюджет анонимного тарифа OpenDota (без OPENDOTA_API_KEY): ~50k
# вызовов/месяц ≈ 1660/сутки на IP, burst-потолок 60/мин (пауза 1.1 с
# между вызовами держит ~54/мин). Дефолты ниже дают до ~1400 вызовов в
# сутки — сбор идёт круглосуточно, а не сгорает за 3-4 часа (runbook
# «витрина не растёт»).
#
# ВАЖНО при пересчёте бюджета: таймлайн-источник тратит НЕ один вызов на
# матч. Его detail_budget = 2 × limit_per_cycle, потому что фильтры
# качества отбраковывают примерно половину кандидатов с вершины
# /parsedMatches. То есть TIMELINE_LIMIT=10 — это до 20 вызовов за цикл,
# а не 10; на 48 циклах в сутки набегает ~1000. Прежняя оценка в этом
# комментарии («1100-1200») множитель не учитывала и занижала итог.
#
# Темп сбора настраивается через env-файл MANTA_TRAIN_ENV, а не правкой
# дефолтов: TIMELINE_LIMIT / PRO_TIMELINE_LIMIT / OPENDOTA_LIMIT и
# соответствующие *_INTERVAL. Текущий выбор владельца и раскладка
# бюджета — docs/HANDOFF.md, «Темп сбора и бюджет квоты».
if ! pgrep -f "collector --source opendota-public" >/dev/null; then
    say "запускаю data-collector (лог: $LOG_DIR/collector.log)"
    (cd apps/data-collector && OPENDOTA_LIMIT="${OPENDOTA_LIMIT:-1}" METRICS_PORT=9105 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m collector --source opendota-public \
            --interval "${PUBLIC_REPLAY_INTERVAL:-3600}" \
            >"$LOG_DIR/collector.log" 2>&1 &)
else
    skip "data-collector"
fi

if ! pgrep -f "collector --source opendota-timeline --interval" >/dev/null; then
    say "запускаю timeline-collector (лог: $LOG_DIR/timeline.log)"
    (cd apps/data-collector && TIMELINE_LIMIT="${TIMELINE_LIMIT:-10}" METRICS_PORT=9108 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m collector --source opendota-timeline \
            --interval "${TIMELINE_INTERVAL:-1800}" \
            >"$LOG_DIR/timeline.log" 2>&1 &)
else
    skip "timeline-collector"
fi

if ! pgrep -f "collector --source opendota-timeline-pro" >/dev/null; then
    say "запускаю pro-timeline-collector (лог: $LOG_DIR/timeline-pro.log)"
    (cd apps/data-collector && TIMELINE_LIMIT="${PRO_TIMELINE_LIMIT:-5}" METRICS_PORT=9110 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m collector --source opendota-timeline-pro \
            --interval "${PRO_TIMELINE_INTERVAL:-3600}" \
            >"$LOG_DIR/timeline-pro.log" 2>&1 &)
else
    skip "pro-timeline-collector"
fi

# STRATZ-коллектор поднимается ТОЛЬКО при заданном токене: без него
# источник падает на старте, и recover плодил бы мёртвый процесс на
# машине, где STRATZ не настроен.
if [ -n "${STRATZ_API_TOKEN:-}" ]; then
    if ! pgrep -f "collector --source stratz-timeline --interval" >/dev/null; then
        say "запускаю stratz-collector (лог: $LOG_DIR/stratz.log)"
        (cd apps/data-collector && STRATZ_LIMIT="${STRATZ_LIMIT:-40}" METRICS_PORT=9115 PYTHONPATH=src:$ROOT/libs \
            nohup python3 -u -m collector --source stratz-timeline \
                --interval "${STRATZ_INTERVAL:-1800}" \
                >"$LOG_DIR/stratz.log" 2>&1 &)
    else
        skip "stratz-collector"
    fi

    # stratz-timeline-pro НЕ ЗАПУСКАЕТСЯ (спринт 93). Про-матчи —
    # дефицит и одновременно эталон гейта, а окно /proMatches меньше,
    # чем мы способны обработать: цикл стабильно давал «собрано 0 из
    # 1000 (дубликат: 238)», то есть источник тратил квоту впустую и
    # при этом забирал половину дефицитного потока у OpenDota, который
    # умеет трек F и networth_total. Весь про-поток теперь идёт через
    # opendota-timeline-pro (см. _detail_split в collector/__main__.py).
else
    printf '   stratz-collector — STRATZ_API_TOKEN не задан, пропуск\n'
fi

# Про-матчи ВГЛУБЬ по лигам (спринт 96). Окно /proMatches — около тысячи
# последних матчей на весь мир, оно выбирается за сутки, и эталон гейта
# перестаёт расти. Лиги дают историю турниров, которой в окне давно нет.
if ! pgrep -f "collector --source opendota-league" >/dev/null; then
    say "запускаю league-collector (лог: $LOG_DIR/league.log)"
    (cd apps/data-collector && TIMELINE_LIMIT="${LEAGUE_LIMIT:-6}" METRICS_PORT=9117 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m collector --source opendota-league \
            --interval "${LEAGUE_INTERVAL:-3600}" \
            >"$LOG_DIR/league.log" 2>&1 &)
else
    skip "league-collector"
fi

if ! pgrep -f "collector --source opendota --interval" >/dev/null; then
    say "запускаю pro-replay-collector (лог: $LOG_DIR/pro-collector.log)"
    # Лимит берётся из env-файла, как и у остальных коллекторов: жёсткая
    # единица здесь молча отменяла документированный OPENDOTA_LIMIT (E4) и
    # держала реплей-путь на одном матче в час независимо от настроек.
    (cd apps/data-collector && OPENDOTA_LIMIT="${OPENDOTA_LIMIT:-1}" METRICS_PORT=9109 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m collector --source opendota \
            --interval "${PRO_REPLAY_INTERVAL:-3600}" \
            >"$LOG_DIR/pro-collector.log" 2>&1 &)
else
    skip "pro-replay-collector"
fi

if ! pgrep -f "python3 -u -m app" >/dev/null; then
    say "запускаю ml-service (gRPC, лог: $LOG_DIR/ml-serve.log)"
    (cd apps/ml-service && PYTHONPATH=src:$ROOT/libs \
        MODEL_PATH="${MODEL_PATH:-registry://win_probability/production}" \
        nohup python3 -u -m app >"$LOG_DIR/ml-serve.log" 2>&1 &)
else
    skip "ml-service"
fi

# Якорь на конец: без него шаблон совпадает с serve_draft/serve_coach/
# serve_features, и similarity молча не поднимался бы, пока жив любой из них.
if ! pgrep -f "python3 -u -m serve$" >/dev/null; then
    say "запускаю similarity (gRPC :50052, лог: $LOG_DIR/similarity.log)"
    (cd apps/similarity && METRICS_PORT=9111 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m serve >"$LOG_DIR/similarity.log" 2>&1 &)
else
    skip "similarity"
fi

if ! pgrep -f "python3 -u -m serve_draft" >/dev/null; then
    say "запускаю draft (gRPC :50053, лог: $LOG_DIR/draft.log)"
    (cd apps/draft && METRICS_PORT=9112 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m serve_draft >"$LOG_DIR/draft.log" 2>&1 &)
else
    skip "draft"
fi

if ! pgrep -f "python3 -u -m serve_coach" >/dev/null; then
    say "запускаю coach (gRPC :50054, лог: $LOG_DIR/coach.log)"
    (cd apps/coach && METRICS_PORT=9113 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m serve_coach >"$LOG_DIR/coach.log" 2>&1 &)
else
    skip "coach"
fi

if ! pgrep -f "python3 -u -m reportgen" >/dev/null; then
    say "запускаю report-generator (лог: $LOG_DIR/report-gen.log)"
    (cd apps/report-generator && METRICS_PORT=9103 PYTHONPATH=src:$ROOT/libs \
        nohup python3 -u -m reportgen >"$LOG_DIR/report-gen.log" 2>&1 &)
else
    skip "report-generator"
fi

# 5. Авто-обучение (+ Telegram-уведомления из env-файла) -----------------------
if ! pgrep -f "python3 -u -m training.auto" >/dev/null; then
    say "запускаю auto-train (лог: $LOG_DIR/wp-auto.log)"
    (cd apps/ml-service && METRICS_PORT=9106 PYTHONPATH=src:$ROOT/libs \
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

# Дашборд — ЕДИНСТВЕННЫЙ процесс, который make stop намеренно не трогает
# (спринт 74: иначе кнопка «Поднять всё» умирает вместе со стеком). Из-за
# этого он же оказался единственным, кто НЕ ПОДХВАТЫВАЛ правки: recover
# пропускал его как «уже работает», и процесс жил со старым кодом через
# любое число git pull. Инцидент 2026-08-04: фиксы спринтов 92 и 96 не
# доехали до страницы вовсе, а на экране висела надпись из версии
# двухдневной давности. Поэтому здесь — сверка кода с процессом.
# `|| true` обязателен: pgrep без совпадений возвращает 1, а скрипт идёт
# под `set -e` — и recover падал ровно в том случае, ради которого этот
# блок писался (дашборд убит вручную перед обновлением). Инцидент
# 2026-08-04, внесён и исправлен в один день.
dash_pid=$(pgrep -f "scripts/dashboard.py" | head -1 || true)
if [ -n "$dash_pid" ]; then
    dash_age=$(ps -o etimes= -p "$dash_pid" 2>/dev/null | tr -d ' ' || true)
    # Процесс мог исчезнуть между pgrep и ps: пустой возраст means «не
    # знаем», и тогда считаем код новее — перезапуск дешевле, чем ещё
    # один день на устаревшей версии.
    started=$(( $(date +%s) - ${dash_age:-0} ))
    if [ "$(stat -c %Y scripts/dashboard.py)" -gt "$started" ]; then
        if [ -n "${MANTA_DASHBOARD_JOB:-}" ]; then
            # recover запущен кнопкой САМОГО дашборда: убить его сейчас
            # значит оборвать эту же задачу на середине.
            printf '   \033[33mВНИМАНИЕ\033[0m дашборд работает на СТАРОМ коде.\n'
            printf '   Перезапустить вручную: pkill -f scripts/dashboard.py && make recover\n'
        else
            say "дашборд старее своего кода — перезапускаю"
            kill "$dash_pid" 2>/dev/null || true
            sleep 1
            dash_pid=""
        fi
    fi
fi
if [ -z "$dash_pid" ] && ! pgrep -f "scripts/dashboard.py" >/dev/null; then
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
for svc in "${SERVICES[@]}"; do
    name="${svc%%|*}"
    # STRATZ без токена не запускается вовсе — показывать его DOWN
    # значило бы объявлять поломкой намеренно выключённый источник.
    if [ "$name" = "stratz-coll." ] && [ -z "${STRATZ_API_TOKEN:-}" ]; then
        continue
    fi
    check "$name" "${svc#*|}"
done
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
