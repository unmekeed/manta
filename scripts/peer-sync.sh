#!/usr/bin/env bash
# Обмен датасетом между машинами через облако (спринт 142).
#
#     make peer-sync                 # втянуть свежие слепки соседей
#     make peer-sync ARGS=--dry-run  # посмотреть, что бы втянулось
#
# ЗАЧЕМ. Две машины собирают РАЗНЫЕ матчи: шардирование по остатку
# match_id (COLLECTOR_SHARD_ID/COUNT) следит, чтобы они не качали одно и
# то же. Пока слепки не встречаются, у каждой машины ровно половина
# датасета, а модель учится на половине.
#
# ЧТО ДЕЛАЕТ. Ровно вторую половину работы backup.sh: тот ПУБЛИКУЕТ свой
# слепок в облако, этот ЗАБИРАЕТ чужие и вливает. Разделение не
# косметическое — публиковать надо после каждого бэкапа, а вливать можно
# реже, и смешать это в один скрипт значило бы получить одно расписание
# на две разные задачи.
#
# КАК ОТЛИЧАЕТ СВОЁ ОТ ЧУЖОГО. По метке машины в имени файла:
# manta-dataset-<метка>-<UTC>.tar (см. MANTA_HOST_LABEL в backup.sh). До
# спринта 142 метки не было, обе машины писали в облако файлы одного вида
# и обмен был невозможен: свой слепок неотличим от чужого, а втянуть свой
# же — это в лучшем случае пустая работа.
#
# ИДЕМПОТЕНТНОСТЬ. Дважды один и тот же файл не вливается: имена уже
# влитых лежат в $BACKUP_DIR/.peer-imported. Сам импорт тоже идемпотентен
# (dataset-sync.sh import вливает только новые match_id), так что защита
# двойная — но без списка каждый прогон качал бы гигабайты заново.
#
# ЧЕГО НЕ ДЕЛАЕТ. Не удаляет ничего в облаке: ротацию ведёт backup.sh, и
# только по своим файлам. Скрипт, который умеет и вливать, и удалять,
# однажды удалит то, что не успел влить.
set -uo pipefail
cd "$(dirname "$0")/.."

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

BACKUP_DIR="${MANTA_BACKUP_DIR:-$HOME/manta-backups}"
CLOUD_REMOTE="${MANTA_CLOUD_REMOTE:-}"
HOST_LABEL=$(printf '%s' "${MANTA_HOST_LABEL:-$(hostname)}" |
    tr -c 'A-Za-z0-9_-' '-' | cut -c1-32)
PEER_HOSTS="${MANTA_PEER_HOSTS:-}"
# Чужие слепки кладутся в ОТДЕЛЬНЫЙ подкаталог, а не рядом со своими, и
# это не про аккуратность. heartbeat.sh считает свежесть бэкапа по самому
# новому файлу в каталоге, backup-drill.sh восстанавливает самый новый, а
# backup.sh по нему же ротирует. Положи слепок соседа рядом — и сторож
# начнёт докладывать о свежем бэкапе, когда свой уже сутки как не
# снимается. Ровно такая тишина однажды длилась тринадцать дней.
PEER_DIR="$BACKUP_DIR/peers"
STATE="$PEER_DIR/.imported"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

tg() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || return 0
    curl -s --max-time 15 \
        "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID:-}" -d "parse_mode=HTML" \
        --data-urlencode "text=$1" >/dev/null || true
}

die() { echo "ОБМЕН НЕ УДАЛСЯ: $1" >&2; tg "🔴 <b>Manta</b>: обмен датасетом — $1"; exit 1; }

[ -n "$CLOUD_REMOTE" ] || die "MANTA_CLOUD_REMOTE не задан — обмену негде идти"
[ -n "$PEER_HOSTS" ] || die "MANTA_PEER_HOSTS не задан — список доверенных машин обязателен"
command -v rclone >/dev/null || die "rclone не установлен"
mkdir -p "$PEER_DIR"
touch "$STATE"

echo "=================================================================="
echo "  Обмен датасетом: я «$HOST_LABEL», облако $CLOUD_REMOTE"
echo "=================================================================="

# Список слепков в облаке. lsf, а не ls: нужен только путь, без размеров и
# дат, которые пришлось бы разбирать.
remote=$(rclone lsf --include 'manta-dataset-*.tar' "$CLOUD_REMOTE" 2>/dev/null) \
    || die "не удалось прочитать $CLOUD_REMOTE"
[ -n "$remote" ] || { echo "В облаке нет ни одного слепка."; exit 0; }

# Чужие — те, чья метка не наша. Разбор именно по префиксу «до последнего
# дефиса перед датой»: метка сама может содержать дефисы.
peers=$(printf '%s\n' "$remote" |
    sed -n 's/^manta-dataset-\(.*\)-[0-9]\{8\}T[0-9]\{4\}\.tar$/\1/p' |
    sort -u | grep -vx "$HOST_LABEL" || true)

# Remote с rclone.conf даёт право ЧИТАТЬ данные, но не должен автоматически
# давать право прислать исполняемый/импортируемый payload. Принимаем только
# явно названные метки; появление неизвестной — отказ всего прогона, а не
# тихий пропуск, иначе опечатка в метке выглядела бы как остановившийся peer.
allowed_peer() {
    local wanted="$1" peer
    local IFS=', '
    for peer in $PEER_HOSTS; do
        [ -n "$peer" ] || continue
        case "$peer" in *[!A-Za-z0-9_-]*) die "небезопасная метка в MANTA_PEER_HOSTS: $peer";; esac
        [ "$peer" = "$wanted" ] && return 0
    done
    return 1
}

for peer in $peers; do
    allowed_peer "$peer" || die "неизвестная метка отправителя: $peer (разрешены: $PEER_HOSTS)"
done

if [ -z "$peers" ]; then
    echo "Чужих слепков нет: в облаке только мои. Сосед ещё не публиковался?"
    exit 0
fi

echo ">> соседи: $(printf '%s' "$peers" | tr '\n' ' ')"
imported=0
skipped=0

for peer in $peers; do
    # Самый свежий слепок соседа. Имя содержит UTC в сортируемом виде
    # (ГГГГММДДTЧЧММ), поэтому лексикографическая сортировка = хронология.
    newest=$(printf '%s\n' "$remote" |
        grep "^manta-dataset-$peer-" | sort | tail -1)
    [ -n "$newest" ] || continue

    if grep -qxF "$newest" "$STATE"; then
        echo "   $peer: $newest уже влит, пропуск"
        skipped=$((skipped + 1))
        continue
    fi

    echo ">> $peer: качаю $newest"
    if [ "$DRY" = "1" ]; then
        echo "   (сухой прогон: не качаю и не вливаю)"
        imported=$((imported + 1))
        continue
    fi

    rclone copy --no-traverse "$CLOUD_REMOTE/$newest" "$PEER_DIR" \
        || die "rclone copy $newest упал"
    [ -s "$PEER_DIR/$newest" ] || die "скачанный $newest пуст"

    echo ">> $peer: вливаю"
    if ./scripts/dataset-sync.sh import "$PEER_DIR/$newest"; then
        # В журнал — только ПОСЛЕ успешного импорта. Записать раньше
        # значило бы навсегда пропустить слепок, на котором импорт упал.
        printf '%s\n' "$newest" >>"$STATE"
        # Архив больше не нужен: всё, что в нём было, уже в базе, а
        # повторный импорт стережёт журнал. Слепки идут гигабайтами, и
        # копить чужие на диске, где уже кончалось место, — плохая идея.
        rm -f "$PEER_DIR/$newest"
        imported=$((imported + 1))
    else
        die "импорт $newest упал"
    fi
done

echo "------------------------------------------------------------------"
echo "Готово: влито $imported, пропущено (уже было) $skipped"

# Ненулевой код, только если делать было что и не получилось ничего.
# «Всё уже влито» — это норма и штатный итог ежедневного прогона.
exit 0
