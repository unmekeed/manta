#!/usr/bin/env bash
# Кэш рангов по account_id (спринт 123).
#
#   ./scripts/ranks.sh seed   --matches 1000   # набрать аккаунты из потока Valve
#   ./scripts/ranks.sh fill   --budget 200     # опросить очередь (по умолчанию OpenDota)
#   ./scripts/ranks.sh fill   --resolver stratz --budget 1000
#   ./scripts/ranks.sh report                  # что накопилось
#   ./scripts/ranks.sh probe                   # что вообще разрешено нашему токену STRATZ
#
# Обёртка нужна ровно за одним: подгрузить ~/manta-train.env. Ключи
# (STEAM_API_KEY, STRATZ_API_TOKEN, OPENDOTA_API_KEY) живут только там,
# в git их нет и быть не может, а `make` окружение сам не читает.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

cmd="${1:-report}"; shift || true

if [ "$cmd" = "seed" ] && [ -z "${STEAM_API_KEY:-}" ]; then
    echo "нет STEAM_API_KEY в $TRAIN_ENV — ключ берётся на" >&2
    echo "https://steamcommunity.com/dev/apikey (аккаунт не должен быть limited)" >&2
    exit 2
fi

cd apps/data-collector
exec env PYTHONPATH="src:$ROOT/libs" python3 -m collector.ranks "$cmd" "$@"
