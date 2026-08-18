#!/usr/bin/env bash
# Учения по восстановлению: слепок → ВРЕМЕННЫЕ базы → сверка строк.
#
#     make backup-drill              # взять свежайший слепок из BACKUP_DIR
#     make backup-drill ARGS=файл.tar
#
# ЗАЧЕМ. Бэкап, который ни разу не восстанавливали, — это не бэкап, а
# предположение. Выгрузка у нас покрыта тестами и алертами (спринт 76), а
# обратный путь `dataset-sync.sh import` до сих пор не запускался НИ ОДНИМ
# тестом и не встречался нигде, кроме собственной справки. Единственный
# тест рядом (test_dataset_sync_empty.py) написан после того, как импорт
# сломался в бою на пустой таблице, — то есть проверяет уже случившееся.
#
# ЧТО ПРОВЕРЯЕТСЯ. Ровно то, ради чего бэкап существует: что из архива
# ПОЛУЧАЮТСЯ ТЕ ЖЕ ДАННЫЕ. Схема накатывается настоящими миграциями,
# импорт идёт настоящим dataset-sync.sh, а потом строки считаются в
# источнике и в копии и сравниваются по каждой таблице.
#
# ЧЕГО УЧЕНИЯ НЕ ТРОГАЮТ. Боевые базы — вообще никак. Поднимаются
# отдельные контейнеры на своих портах, и есть защита от запуска против
# production (см. ниже). По окончании контейнеры сносятся, даже если
# прогон упал.
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${MANTA_BACKUP_DIR:-$HOME/manta-backups}"
CH_IMAGE="clickhouse/clickhouse-server:24.8"
PG_IMAGE="postgres:16"
DRILL_CH="manta-drill-clickhouse"
DRILL_PG="manta-drill-postgres"
# Порты заведомо в стороне от боевых (5432/8123/9000), чтобы учения можно
# было гонять на работающей машине, не останавливая сбор.
DRILL_PG_PORT="${DRILL_PG_PORT:-55432}"
DRILL_CH_PORT="${DRILL_CH_PORT:-58123}"
PASS="drill_only_$$"

RED=$'\033[31m'; GREEN=$'\033[32m'; OFF=$'\033[0m'
ok()   { echo "    ${GREEN}OK${OFF}   $1"; }
bad()  { echo "    ${RED}FAIL${OFF} $1"; FAILED=$((FAILED + 1)); }
FAILED=0

# Защита от катастрофы. Имена временных контейнеров заданы константами
# выше, но переменные окружения могли прийти из чужого шелла — а импорт
# в боевую базу с чужого слепка затёр бы курсоры коллекторов и отчёты.
for var in CH_CONTAINER PG_CONTAINER; do
    val="${!var:-}"
    case "$val" in
        ""|manta-drill-*) ;;
        *) echo "ОТКАЗ: $var=$val указывает не на временный контейнер." >&2
           echo "Учения работают только с manta-drill-*. Сбрось переменную." >&2
           exit 2;;
    esac
done

cleanup() {
    echo ">> убираю временные контейнеры"
    docker rm -f "$DRILL_CH" "$DRILL_PG" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# -- 1. слепок -----------------------------------------------------------------

archive="${1:-}"
if [ -z "$archive" ]; then
    archive=$(ls -1t "$BACKUP_DIR"/manta-dataset-*.tar 2>/dev/null | head -1 || true)
    [ -n "$archive" ] || {
        echo "нет слепков в $BACKUP_DIR — сначала ./scripts/backup.sh" >&2
        exit 2; }
fi
[ -s "$archive" ] || { echo "архив пуст или не найден: $archive" >&2; exit 2; }
echo ">> слепок: $archive ($(du -h "$archive" | cut -f1))"

# -- 2. временные базы ---------------------------------------------------------

echo ">> поднимаю временные базы"
docker rm -f "$DRILL_CH" "$DRILL_PG" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_PG" \
    -e POSTGRES_USER=dota -e POSTGRES_PASSWORD="$PASS" -e POSTGRES_DB=manta \
    -p "127.0.0.1:$DRILL_PG_PORT:5432" "$PG_IMAGE" >/dev/null
docker run -d --name "$DRILL_CH" \
    -e CLICKHOUSE_USER=dota -e CLICKHOUSE_PASSWORD="$PASS" \
    -e CLICKHOUSE_DB=manta \
    -p "127.0.0.1:$DRILL_CH_PORT:8123" "$CH_IMAGE" >/dev/null

echo -n ">> жду готовности"
for _ in $(seq 60); do
    if docker exec "$DRILL_PG" pg_isready -U dota -d manta >/dev/null 2>&1 &&
       docker exec "$DRILL_CH" clickhouse-client --user dota \
            --password "$PASS" -q "SELECT 1" >/dev/null 2>&1; then
        echo " готовы"; break
    fi
    echo -n "."; sleep 2
done

# -- 3. схема настоящими миграциями --------------------------------------------

echo ">> накатываю миграции на временные базы"
CH_CONTAINER="$DRILL_CH" CLICKHOUSE_PASSWORD="$PASS" \
    ./scripts/ch-migrate.sh >/dev/null
PGPASSWORD="$PASS" POSTGRES_PORT="$DRILL_PG_PORT" \
    ./scripts/pg-migrate.sh >/dev/null

# -- 4. импорт настоящим кодом -------------------------------------------------

echo ">> восстанавливаю слепок"
CH_CONTAINER="$DRILL_CH" PG_CONTAINER="$DRILL_PG" \
CLICKHOUSE_PASSWORD="$PASS" PGPASSWORD="$PASS" \
    ./scripts/dataset-sync.sh import "$archive"

# -- 5. сверка -----------------------------------------------------------------
#
# Сверяем ДВАЖДЫ и по-разному.
#
# Основное — против meta.json из самого архива: он несёт счётчики на
# момент выгрузки и есть всегда, в том числе на машине, где боевой базы
# нет вовсе (а это и есть настоящее восстановление после потери).
#
# Дополнительно — против живого источника, если он под рукой. Это ловит
# то, чего манифест не видит: слепок мог быть снят с уже испорченной
# базы, и сам с собой он сойдётся.
#
# Первая версия сверяла ТОЛЬКО с источником и, не найдя его, печатала
# «сравнить не с чем» одиннадцать раз, после чего объявляла учения
# пройденными. Ноль сверок — это не успех, это отсутствие проверки.

CHECKED=0
check() {           # check <что> <ожидалось> <получено>
    if [ "$2" = "?" ] || [ -z "$2" ]; then
        return 0
    fi
    CHECKED=$((CHECKED + 1))
    if [ "$2" = "$3" ]; then
        ok "$1: $3"
    elif [ "$3" = "?" ] || [ -z "$3" ]; then
        # Отличать «запрос не выполнился» от «пришло другое число» важно:
        # первое означает сломанный ЗАПРОС учений, второе — испорченный
        # бэкап. На первом живом прогоне это была ровно первая беда
        # (забытый --database), и сообщение «получено ?» увело бы искать
        # проблему в данных.
        bad "$1: ожидалось $2, но ЗАПРОС НЕ ВЫПОЛНИЛСЯ (не бэкап виноват)"
    else
        bad "$1: ожидалось $2, получено $3"
    fi
}

# --database обязателен. Без него clickhouse-client ищет таблицу в базе
# по умолчанию для пользователя, а наши живут в `manta` — dataset-sync
# поэтому и пишет всюду `manta.MatchTimelineFeatures` полным именем.
# Первый живой прогон дал из-за этого «ожидалось 2070, получено ?»: данные
# восстановились верно, а сломан был запрос учений.
CH_DB="${CLICKHOUSE_DB:-manta}"
dst_ch() { docker exec "$DRILL_CH" clickhouse-client \
    --user dota --password "$PASS" --database "$CH_DB" \
    -q "$1" 2>/dev/null || echo "?"; }
dst_pg() { docker exec "$DRILL_PG" psql -U dota -d manta \
    -tAc "$1" 2>/dev/null || echo "?"; }
src_ch() { docker exec "${SRC_CH:-manta-clickhouse-1}" clickhouse-client \
    --user dota --password "${SRC_CH_PASS:-dota_dev_password}" \
    --database "$CH_DB" -q "$1" 2>/dev/null || echo "?"; }
src_pg() { docker exec "${SRC_PG:-manta-postgres-1}" psql -U dota -d manta \
    -tAc "$1" 2>/dev/null || echo "?"; }

meta_dir=$(mktemp -d)
tar -xf "$archive" -C "$meta_dir" --wildcards '*meta.json' 2>/dev/null || true
meta=$(find "$meta_dir" -name meta.json | head -1)
want() {            # значение поля из meta.json архива
    [ -n "$meta" ] || { echo "?"; return; }
    python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],'?'))" \
        "$meta" "$1" 2>/dev/null || echo "?"
}

echo
echo "== сверка с манифестом архива (эталон на момент выгрузки)"
if [ -z "$meta" ]; then
    bad "в архиве нет meta.json — сверять не с чем"
else
    check "матчей в витрине" "$(want matches_in_mart)" \
          "$(dst_ch 'SELECT count(DISTINCT match_id) FROM MatchTimelineFeatures FINAL')"
    check "collectedmatches" "$(want collected)" \
          "$(dst_pg 'SELECT count(*) FROM collectedmatches')"
    check "matchreports" "$(want reports)" \
          "$(dst_pg 'SELECT count(*) FROM matchreports')"
fi
rm -rf "$meta_dir"

echo
echo "== живая база против слепка (справочно, на вердикт не влияет)"
# Сравнивать слепок с ТЕКУЩИМ состоянием как «должно совпасть» — ошибка.
# Слепок снят когда-то; с тех пор шёл сбор, и живая база законно ушла
# вперёд. Первый живой прогон на двухнедельном архиве дал из-за этого
# девять «провалов» и вердикт «восстановить ровно то же нельзя» — при
# полностью сошедшемся манифесте.
#
# Полезное направление здесь ровно одно и обратное: живая база НЕ ДОЛЖНА
# отставать от слепка. Меньше строк, чем в старой копии, — это признак
# потери данных, и вот об этом сказать стоит.
LOST=0
grew() {            # grew <что> <в слепке> <в живой базе>
    if [ "$3" = "?" ] || [ -z "$3" ]; then return 0; fi
    if [ "$3" -lt "$2" ] 2>/dev/null; then
        echo "    ${RED}ОТСТАЁТ${OFF} $1: в слепке $2, в живой базе $3"
        LOST=$((LOST + 1))
    else
        echo "    ок      $1: слепок $2 → живая база $3"
    fi
}

src_probe=$(src_ch "SELECT count() FROM MatchTimelineFeatures")
if [ "$src_probe" = "?" ]; then
    echo "    --   боевая база недоступна: пропущено (на вердикт не влияет)"
else
    for t in MatchTimelineFeatures PlayerMatchFeatures MatchDraft MatchEvents \
             MatchFights MatchMapCells MatchHeroTimings \
             EconomyTimeline PositionSnapshots; do
        grew "$t" "$(dst_ch "SELECT count() FROM $t")" "$(src_ch "SELECT count() FROM $t")"
    done
    for t in collectedmatches matchreports; do
        grew "$t" "$(dst_pg "SELECT count(*) FROM $t")" "$(src_pg "SELECT count(*) FROM $t")"
    done
    if [ "$LOST" -gt 0 ]; then
        echo
        echo "    ${RED}ВНИМАНИЕ${OFF}: живая база отстаёт от слепка по $LOST таблицам."
        echo "    Это не провал учений, но повод разобраться: данные могли"
        echo "    быть потеряны уже ПОСЛЕ снятия этой копии."
    fi
fi

echo
if [ "$CHECKED" -eq 0 ]; then
    # Главная защита от самообмана. Ноль выполненных сверок означает, что
    # мы ничего не проверили, — а первая версия в этом случае бодро
    # печатала «учения пройдены».
    echo "${RED}>> УЧЕНИЯ НЕ ПРОВЕДЕНЫ: ни одной сверки не выполнено${OFF}"
    echo "Слепок развернулся, но сверить его было не с чем: ни манифеста"
    echo "в архиве, ни доступной боевой базы. Это не успех."
    exit 1
elif [ "$FAILED" -eq 0 ]; then
    echo "${GREEN}>> УЧЕНИЯ ПРОЙДЕНЫ${OFF}: сверок $CHECKED, расхождений нет."
else
    echo "${RED}>> УЧЕНИЯ ПРОВАЛЕНЫ: расхождений $FAILED из $CHECKED сверок${OFF}"
    echo "Бэкап есть, но восстановить из него ровно то же — нельзя."
    exit 1
fi
