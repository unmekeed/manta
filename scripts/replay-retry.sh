#!/usr/bin/env bash
# Вернуться к припаркованным реплеям (спринт 153).
#
# ЗАЧЕМ. Матчи, чей реплей не удалось взять, больше не теряются: курсор
# уходит вперёд (очередь важнее одного матча), а сам матч записывается в
# ParkedReplays. Этот скрипт — вторая половина обещания: когда маршрут
# появился, к списку надо вернуться.
#
# Запускается ВРУЧНУЮ и осознанно. В расписание не ставится: пока
# маршрута нет, повтор просто сожжёт бюджет вызовов на те же таймауты, а
# бюджет у реплейного источника 50 в сутки.
#
#   ./scripts/replay-retry.sh            # показать, что стоит в парковке
#   ./scripts/replay-retry.sh --run      # попробовать забрать
set -euo pipefail

CONTAINER="${REPLAY_CONTAINER:-manta-pro-replay-collector-1}"

die() { printf 'ОСТАНОВ: %s\n' "$1" >&2; exit 1; }

command -v docker >/dev/null || die "docker не установлен"
docker inspect "$CONTAINER" >/dev/null 2>&1 \
    || die "контейнер $CONTAINER не найден: подними стек или задай REPLAY_CONTAINER"

# Через docker exec, а не docker run: у живого контейнера уже есть всё
# окружение — Postgres, Kafka, MinIO, ключи, — и собирать его заново
# значило бы завести вторую копию настроек, которая разъедется с первой.
if [ "${1:-}" = "--run" ]; then
    printf '>> повтор припаркованных (лимит PARKED_LIMIT=%s)\n' \
        "${PARKED_LIMIT:-5}"
    exec docker exec -e PARKED_LIMIT="${PARKED_LIMIT:-5}" "$CONTAINER" \
        python -m collector --source parked --once
fi

printf '>> парковка (запуск повтора: %s --run)\n' "$0"
exec docker exec "$CONTAINER" python -m collector --parked-report
