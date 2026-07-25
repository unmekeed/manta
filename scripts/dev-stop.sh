#!/usr/bin/env bash
# make stop — остановить ХОСТОВЫЕ процессы платформы. Контейнеры и данные
# не трогаются вообще: ни docker stop, ни compose down, ни volumes.
#
# Зачем отдельный скрипт. dev-recover.sh запускает сервис только если тот
# ещё не жив (`if ! pgrep`), поэтому после `git pull` он НЕ перезапускает
# уже работающие процессы — они продолжают крутить старый код. Симптом
# самый неприятный: всё зелёное, а новые фичи не собираются, потому что
# коллектор в памяти остался прежним. Штатный порядок обновления:
#
#     make stop && git pull && make migrate && make recover
#
# Модель отдельно останавливать не нужно и незачем: артефакт несёт свой
# набор фич (predictors.WinProbability.features), поэтому и старая, и
# новая версия корректно работают с обновлённым кодом.
set -uo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\033[36m>>\033[0m %s\n' "$*"; }
gone() { printf '   %-18s %s\n' "$1" "$2"; }

# Порядок: сначала писатели (коллекторы, парсер, extractor), потом
# читатели/сервинг — чтобы никто не писал в уже упавшего соседа.
PATTERNS=(
    "collector --source opendota-public:data-collector"
    "collector --source opendota-timeline --interval:timeline-coll."
    "collector --source opendota-timeline-pro:pro-timeline"
    "collector --source opendota --interval:pro-replay"
    "^/tmp/parser-svc:parser-svc"
    "python3 -u -m extractor:feature-extractor"
    "python3 -u -m training.auto:auto-train"
    "python3 -u -m reportgen:report-generator"
    "python3 -u -m serve_features:feature-store"
    "python3 -u -m serve_coach:coach"
    "python3 -u -m serve_draft:draft"
    # Якорь на конец обязателен: без него шаблон ловит и serve_draft,
    # и serve_coach, и serve_features.
    "python3 -u -m serve$:similarity"
    "python3 -u -m app:ml-service"
    "^/tmp/api-gateway:api-gateway"
    "vite --host:frontend"
    "scripts/dashboard.py:dashboard"
)

say "останавливаю хостовые процессы (контейнеры и данные не трогаю)"
for entry in "${PATTERNS[@]}"; do
    pat="${entry%:*}"; name="${entry##*:}"
    if ! pgrep -f "$pat" >/dev/null; then
        gone "$name" "уже не запущен"
        continue
    fi
    pkill -f "$pat"
    # Даём дописать текущую пачку в ClickHouse/Kafka: SIGKILL сразу —
    # это оборванный INSERT и потерянный оффсет консьюмера.
    for _ in $(seq 20); do
        pgrep -f "$pat" >/dev/null || break
        sleep 0.5
    done
    if pgrep -f "$pat" >/dev/null; then
        pkill -9 -f "$pat"
        gone "$name" "остановлен принудительно (не вышел за 10с)"
    else
        gone "$name" "остановлен"
    fi
done

echo
say "инфраструктура продолжает работать:"
docker compose -f deployments/docker-compose.yml ps --format \
    '   {{.Service}}\t{{.Status}}' 2>/dev/null || true
echo
say "дальше: git pull && make migrate && make recover"
