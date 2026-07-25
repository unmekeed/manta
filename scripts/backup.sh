#!/usr/bin/env bash
# Ежедневный бэкап датасета с ротацией (E1 роадмапа).
#
#   ./scripts/backup.sh                    # снять слепок в каталог бэкапов
#   MANTA_BACKUP_DIR=/mnt/d/manta-backups ./scripts/backup.sh
#
# Зачем: docker-volume — единственная копия датасета. Он уже терялся
# (пересоздание volume, «Clean data» в Docker Desktop, DROP в миграции —
# см. docs/HANDOFF.md). Ежедневный слепок ограничивает потерю одними
# сутками сбора вместо всего датасета.
#
# Куда: MANTA_BACKUP_DIR (по умолчанию ~/manta-backups). Держать бэкапы
# на диске Windows (/mnt/c, /mnt/d) полезнее, чем в файловой системе WSL:
# они переживут wsl --unregister и переустановку дистрибутива.
#
# Ротация: KEEP_DAYS (по умолчанию 7) — старые слепки удаляются ПОСЛЕ
# успешного создания нового, поэтому сбой не оставляет без копий.
#
# Уведомления: при сбое (и при первом успехе после сбоя) шлётся сообщение
# в Telegram, если заданы TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — молчаливо
# сломавшийся бэкап хуже отсутствующего.
set -uo pipefail
cd "$(dirname "$0")/.."

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

BACKUP_DIR="${MANTA_BACKUP_DIR:-$HOME/manta-backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
STATE="$BACKUP_DIR/.last-status"
mkdir -p "$BACKUP_DIR"

tg() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || return 0
    curl -s --max-time 15 \
        "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID:-}" -d "parse_mode=HTML" \
        --data-urlencode "text=$1" >/dev/null || true
}

fail() {
    echo "БЭКАП НЕ УДАЛСЯ: $1" >&2
    [ "$(cat "$STATE" 2>/dev/null)" = "fail" ] || \
        tg "🔴 <b>Manta</b>: бэкап датасета не удался — $1"
    echo fail >"$STATE"
    exit 1
}

docker ps --format '{{.Names}}' 2>/dev/null | grep -q manta-clickhouse-1 \
    || fail "ClickHouse не запущен (make recover?)"

out="$BACKUP_DIR/manta-dataset-$(date -u +%Y%m%dT%H%M).tar"
echo ">> слепок: $out"
./scripts/dataset-sync.sh export "$out" || fail "dataset-sync export упал"
[ -s "$out" ] || fail "архив пустой"

# Ротация — только после успешного слепка.
removed=$(find "$BACKUP_DIR" -maxdepth 1 -name 'manta-dataset-*.tar' \
    -mtime "+$KEEP_DAYS" -print -delete | wc -l)

size=$(du -h "$out" | cut -f1)
kept=$(find "$BACKUP_DIR" -maxdepth 1 -name 'manta-dataset-*.tar' | wc -l)
echo ">> готово: $size, слепков в каталоге: $kept (удалено старых: $removed)"

if [ "$(cat "$STATE" 2>/dev/null)" = "fail" ]; then
    tg "✅ <b>Manta</b>: бэкап датасета снова проходит ($size)"
fi
echo ok >"$STATE"
