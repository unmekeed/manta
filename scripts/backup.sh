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
# Оффсайт (спринт 76): MANTA_CLOUD_REMOTE — rclone-ремоут вида
# "gdrive-crypt:manta-backups". Если задан, после локального слепка тот же
# tar уходит в облако (Google Drive/S3/что угодно — зависит от настройки
# rclone) с той же ротацией KEEP_DAYS. Не задан — оффсайт выключен, как и
# все опциональные фичи проекта. Локальный бэкап на диске той же машины —
# единственная копия ровно до тех пор, пока машина жива: переустановка
# системы уносит и датасет, и бэкап. Оффсайт это чинит.
#
# ВАЖНО (PII): слепок содержит никнеймы (PlayerMatchFeatures.player_name).
# Слать их в чужое облако в открытом виде нельзя. Два способа закрыть:
#   1) rclone crypt-ремоут — клиентское шифрование, облако видит только
#      шифртекст (рекомендуется, см. docs/HANDOFF.md);
#   2) MANTA_PII_MODE=pseudonymize — в слепке уже хеши, а не имена.
#
# Уведомления: при сбое (и при первом успехе после сбоя) шлётся сообщение
# в Telegram, если заданы TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — молчаливо
# сломавшийся бэкап хуже отсутствующего. Сбой оффсайта — отдельный
# оранжевый алерт: локальный слепок при этом цел, это деградация, а не
# полная потеря.
set -uo pipefail
cd "$(dirname "$0")/.."

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

BACKUP_DIR="${MANTA_BACKUP_DIR:-$HOME/manta-backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
# Метка машины в имени слепка (спринт 142). Без неё две машины кладут в
# один облачный каталог файлы с одинаковым шаблоном имени, и обмен между
# ними невозможен в принципе: нельзя отличить свой слепок от чужого, а
# значит нельзя и втянуть чужой. Санитайзим: имя уходит в путь rclone, и
# пробел или слэш в hostname сломали бы его молча.
HOST_LABEL=$(printf '%s' "${MANTA_HOST_LABEL:-$(hostname)}" |
    tr -c 'A-Za-z0-9_-' '-' | cut -c1-32)
CLOUD_REMOTE="${MANTA_CLOUD_REMOTE:-}"
STATE="$BACKUP_DIR/.last-status"
CLOUD_STATE="$BACKUP_DIR/.last-cloud-status"
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

cloud_fail() {
    # Оффсайт упал, но локальный слепок УЖЕ на диске — это деградация, а не
    # потеря. Оранжевый алерт, отдельное состояние, выход 2 (не 1): чтобы
    # планировщик отличал «нет копий вообще» от «нет только оффсайта».
    echo "ОФФСАЙТ НЕ УДАЛСЯ: $1" >&2
    [ "$(cat "$CLOUD_STATE" 2>/dev/null)" = "fail" ] || \
        tg "🟠 <b>Manta</b>: оффсайт-бэкап не удался — $1 (локальный слепок цел)"
    echo fail >"$CLOUD_STATE"
    exit 2
}

upload_cloud() {
    local file="$1"
    [ -n "$CLOUD_REMOTE" ] || return 0      # оффсайт не настроен — это норма
    # rclone задан ремоутом, но не установлен — это «настроено, но сломано»,
    # а не «выключено»: молча пропускать нельзя, иначе оффсайта нет, а всё
    # выглядит зелёным.
    command -v rclone >/dev/null || \
        cloud_fail "MANTA_CLOUD_REMOTE задан, но rclone не установлен (curl https://rclone.org/install.sh | sudo bash)"
    echo ">> оффсайт: rclone copy → $CLOUD_REMOTE"
    rclone copy --no-traverse "$file" "$CLOUD_REMOTE" \
        || cloud_fail "rclone copy в $CLOUD_REMOTE упал"
    # Ротация в облаке — тем же окном KEEP_DAYS и только после успешной
    # загрузки. --include ограничивает удаление нашими слепками: если ремоут
    # указывает в общую папку, чужие файлы не трогаем.
    # Ротация трогает ТОЛЬКО свои слепки. Раньше маска была общая, и при
    # обмене между машинами машина с меньшим KEEP_DAYS удаляла бы чужие
    # слепки — то есть чинила бы себе место за счёт чужой истории.
    rclone delete --min-age "${KEEP_DAYS}d" \
        --include "manta-dataset-$HOST_LABEL-*.tar" \
        "$CLOUD_REMOTE" 2>/dev/null || true
    [ "$(cat "$CLOUD_STATE" 2>/dev/null)" = "fail" ] && \
        tg "✅ <b>Manta</b>: оффсайт-бэкап снова проходит"
    echo ok >"$CLOUD_STATE"
}

docker ps --format '{{.Names}}' 2>/dev/null | grep -q manta-clickhouse-1 \
    || fail "ClickHouse не запущен (make recover?)"

out="$BACKUP_DIR/manta-dataset-$HOST_LABEL-$(date -u +%Y%m%dT%H%M).tar"
echo ">> слепок: $out"
./scripts/dataset-sync.sh export "$out" || fail "dataset-sync export упал"
[ -s "$out" ] || fail "архив пустой"

# Ротация — только после успешного слепка и только по СВОИМ файлам: в
# каталоге могут лежать втянутые слепки соседа (см. peer-sync.sh), и
# удалять их по своему окну хранения мы не вправе.
removed=$(find "$BACKUP_DIR" -maxdepth 1 -name "manta-dataset-$HOST_LABEL-*.tar" \
    -mtime "+$KEEP_DAYS" -print -delete | wc -l)

size=$(du -h "$out" | cut -f1)
kept=$(find "$BACKUP_DIR" -maxdepth 1 -name 'manta-dataset-*.tar' | wc -l)
echo ">> готово: $size, слепков в каталоге: $kept (удалено старых: $removed)"

if [ "$(cat "$STATE" 2>/dev/null)" = "fail" ]; then
    tg "✅ <b>Manta</b>: бэкап датасета снова проходит ($size)"
fi
echo ok >"$STATE"

# Оффсайт — ПОСЛЕ фиксации локального успеха: если облако упадёт, локальная
# копия уже гарантированно есть и её состояние записано.
upload_cloud "$out"
