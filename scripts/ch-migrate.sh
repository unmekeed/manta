#!/usr/bin/env bash
# Идемпотентный прогон ClickHouse-миграций через журнал (спринт 54).
#
# Инцидент: миграция 002 делает DROP TABLE ReplayEvents перед CREATE (это
# была одноразовая правка схемы). Но make recover прогонял migrate-ch на
# КАЖДОМ запуске (спринт 49), поэтому 002 стирала весь combat-лог при
# каждом recover. PositionSnapshots/EconomyTimeline/витрина создаются через
# IF NOT EXISTS и DROP над ними нет — уцелевали; терялся именно ReplayEvents,
# а с ним обучающие данные Death-Risk/Laning и атрибуция ошибок.
#
# Решение как у PG (scripts/pg-migrate.sh): каждый файл применяется РОВНО
# один раз, применённые запоминаются в manta.SchemaMigrations.
#
# Baseline на развёрнутой базе без журнала помечает применёнными ТОЛЬКО
# разрушительные файлы (с DROP TABLE) — их повторный прогон и есть баг.
# Остальные (CREATE/ALTER ... IF NOT EXISTS) идемпотентны и прогоняются
# честно: машина, отставшая на несколько миграций, доедет до актуальной
# схемы, а не получит фиктивно «применённые» записи в журнале.
set -euo pipefail
cd "$(dirname "$0")/.."

# Пароли берутся из файла секретов, если он есть (спринт 143).
#
# ЗАЧЕМ. На VPS пароль генерируется при установке и лежит в
# ~/manta-train.env. Мигратор читал только окружение вызывающего и без
# него молча подставлял дев-умолчание из репозитория — то есть заведомо
# неверный пароль. На домашней машине это никогда не всплывало, потому
# что там дев-умолчание и есть настоящий пароль.
#
# УЖЕ ЗАДАННОЕ ОКРУЖЕНИЕ ПОБЕЖДАЕТ. Учения по восстановлению передают
# свой пароль явно (backup-drill.sh), и затирание его боевым увело бы
# миграции временных баз не туда. Поэтому не `set -a; .`, а поимённая
# подстановка только того, чего ещё нет.
TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
if [ -f "$TRAIN_ENV" ]; then
    while IFS='=' read -r _k _v; do
        case "$_k" in ''|'#'*|*' '*) continue ;; esac
        [ -n "${!_k:-}" ] || export "$_k=$_v"
    done <"$TRAIN_ENV"
fi

CH_DB="${CLICKHOUSE_DB:-manta}"
# Контейнер параметризован, как в dataset-sync.sh: учения по
# восстановлению (scripts/backup-drill.sh) накатывают схему на
# ВРЕМЕННУЮ базу тем же кодом. Дубль логики миграций разошёлся бы
# с оригиналом ровно тогда, когда проверять восстановление важнее
# всего.
CH_CONTAINER="${CH_CONTAINER:-manta-clickhouse-1}"
# ВАЖНО: без -i. С проброшенным stdin clickhouse-client на запросе INSERT
# начинает читать секцию данных из stdin и висит вечно, если stdin — не
# закрытый терминал (ровно так recover замирал на записи в журнал).
# Файлы миграций подаются отдельным вызовом с -i, где stdin нужен по делу.
CLI=(docker exec "$CH_CONTAINER" clickhouse-client
     --user "${CLICKHOUSE_USER:-dota}"
     --password "${CLICKHOUSE_PASSWORD:-dota_dev_password}"
     --connect_timeout 10 --receive_timeout 300)

# Контейнер должен быть запущен. Без проверки ошибка выглядит как
# «Error: No such container» посреди вывода, и непонятно, чей это
# контейнер и почему его ждали (спринт 143 — по образцу pg-migrate.sh).
docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CH_CONTAINER" || {
    echo "ОСТАНОВ: контейнер $CH_CONTAINER не запущен — накатывать некуда" >&2
    exit 1
}

q() { "${CLI[@]}" --database "$CH_DB" --query "$1" </dev/null; }

q "CREATE TABLE IF NOT EXISTS SchemaMigrations (
     filename String, applied_at DateTime DEFAULT now())
   ENGINE = MergeTree() ORDER BY filename"

n_applied=$(q "SELECT count() FROM SchemaMigrations")
has_re=$(q "SELECT count() FROM system.tables
            WHERE database = '$CH_DB' AND name = 'ReplayEvents'")
if [ "$n_applied" = "0" ] && [ "$has_re" != "0" ]; then
    echo ">> CH: развёрнутая база без журнала — baseline разрушительных миграций"
    for f in infra/migrations/clickhouse/*.sql; do
        grep -q "DROP TABLE" "$f" || continue
        b=$(basename "$f")
        q "INSERT INTO SchemaMigrations (filename) VALUES ('$b')"
        echo "   baseline $b (содержит DROP — повторно не прогоняем)"
    done
fi

for f in infra/migrations/clickhouse/*.sql; do
    b=$(basename "$f")
    if [ "$(q "SELECT count() FROM SchemaMigrations WHERE filename = '$b'")" != "0" ]
    then
        echo "   $b — применена, пропуск"
        continue
    fi
    echo ">> $b"
    docker exec -i "$CH_CONTAINER" clickhouse-client \
        --user "${CLICKHOUSE_USER:-dota}" \
        --password "${CLICKHOUSE_PASSWORD:-dota_dev_password}" \
        --multiquery < "$f"
    q "INSERT INTO SchemaMigrations (filename) VALUES ('$b')"
done
