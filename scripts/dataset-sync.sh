#!/usr/bin/env bash
# Синхронизация датасета между машинами (E2 роадмапа: облако ↔ локалка).
#
#   ./scripts/dataset-sync.sh export [файл.tar]   # снять слепок датасета
#   ./scripts/dataset-sync.sh verify файл.tar     # проверить без импорта
#   ./scripts/dataset-sync.sh import файл.tar     # идемпотентно влить слепок
#
# ЧТО переносится и ПОЧЕМУ — в scripts/lib/dataset-tables.sh, одним
# списком на весь проект (спринт 156). Здесь только механика.
#
# Импорт можно повторять сколько угодно: счётчики не растут.
set -euo pipefail

# shellcheck source=lib/dataset-tables.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/dataset-tables.sh"

CH="${CH_CONTAINER:-manta-clickhouse-1}"
PG="${PG_CONTAINER:-manta-postgres-1}"
CH_USER="${CLICKHOUSE_USER:-dota}"
CH_PASS="${CLICKHOUSE_PASSWORD:-dota_dev_password}"
PG_USER="${POSTGRES_USER:-dota}"
PG_DB="${POSTGRES_DB:-manta}"
MANIFEST="MANIFEST.sha256"


chq() { docker exec -i "$CH" clickhouse-client --user "$CH_USER" --password "$CH_PASS" -q "$1"; }
pgq() { docker exec -i "$PG" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -q "$@"; }

# Колонки таблицы в порядке объявления. Кладутся В АРХИВ рядом с CSV, и
# при импорте берутся оттуда, а не из живой схемы.
#
# Иначе обмен ломается ровно тогда, когда он нужнее всего: две машины
# почти никогда не стоят на одном коммите, и первая же миграция,
# добавившая колонку, разводит их схемы. Дамп с четырьмя колонками,
# влитый в таблицу с пятью, даёт «missing data for column» — то есть
# перенос перестаёт работать при каждом изменении схемы и чинится только
# одновременным обновлением обеих машин. Миграция 012 (has_replay) такую
# пару и развела.
pg_columns() {
    pgq -t -A -c "SELECT string_agg(quote_ident(column_name), ','
                                    ORDER BY ordinal_position)
                  FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = '$1'"
}

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,15p'; exit 1; }

# Единственные имена, которые имеют право находиться в слепке. Архивы
# приходят с другой машины через общий remote, поэтому проверка расширения
# недостаточна: лишний файл не должен даже попасть во временный каталог.
snapshot_names() {
    local t
    printf '%s\n' meta.json "$MANIFEST"
    for t in "${REPLACING_TABLES[@]}" "${RAW_TABLES[@]}"; do
        printf '%s.native.gz\n' "$t"
    done
    for t in "${PG_TABLES[@]}"; do
        printf '%s.csv.gz\n%s.cols\n' "$t" "$t"
    done
}

# Проверяет структуру и SHA-256 ДО распаковки. Python здесь уже является
# зависимостью dataset-sync.sh (csv_field_count) и позволяет проверять типы
# tar-members без хрупкого разбора человекочитаемого `tar -tvf`.
verify_snapshot() {
    local archive="$1" allowed
    command -v python3 >/dev/null 2>&1 || {
        echo "ОШИБКА: для проверки слепка нужен python3" >&2; return 1; }
    allowed=$(snapshot_names)
    SNAPSHOT_ALLOWED="$allowed" python3 - "$archive" "$MANIFEST" <<'PY'
import hashlib, os, pathlib, re, sys, tarfile

archive, manifest_name = sys.argv[1:]
allowed = set(os.environ["SNAPSHOT_ALLOWED"].splitlines())

def clean(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")

try:
    with tarfile.open(archive, "r:*") as tf:
        files = {}
        for member in tf.getmembers():
            name = clean(member.name)
            if not name and member.isdir():
                continue
            path = pathlib.PurePosixPath(name)
            if (not name or path.is_absolute() or ".." in path.parts or
                    len(path.parts) != 1):
                raise ValueError(f"опасный путь в архиве: {member.name!r}")
            if not member.isfile():
                raise ValueError(f"разрешены только обычные файлы: {member.name!r}")
            if name not in allowed:
                raise ValueError(f"лишний файл в архиве: {name}")
            if name in files:
                raise ValueError(f"дублирующийся файл в архиве: {name}")
            files[name] = member

        if manifest_name not in files:
            raise ValueError(f"нет обязательного {manifest_name}")
        if "meta.json" not in files:
            raise ValueError("нет обязательного meta.json")

        raw = tf.extractfile(files[manifest_name]).read().decode("utf-8")
        declared = {}
        for line in raw.splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})\s+[ *]?(?:\./)?([^/]+)", line)
            if not match:
                raise ValueError(f"неверная строка manifest: {line!r}")
            digest, name = match.groups()
            if name == manifest_name or name in declared:
                raise ValueError(f"неверное или повторное имя в manifest: {name}")
            declared[name] = digest.lower()

        actual = set(files) - {manifest_name}
        if set(declared) != actual:
            missing = sorted(actual - set(declared))
            extra = sorted(set(declared) - actual)
            raise ValueError(f"manifest не совпадает с архивом: не описаны={missing}, отсутствуют={extra}")

        for name, expected in declared.items():
            digest = hashlib.sha256()
            source = tf.extractfile(files[name])
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"SHA-256 не совпал: {name}")
except (OSError, tarfile.TarError, UnicodeError, ValueError) as exc:
    print(f"ОШИБКА: недоверенный слепок: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

export_dataset() {
    local out="${1:-manta-dataset-$(date -u +%Y%m%dT%H%M).tar}"

    # Проверяем цель ДО дампов, а не после. Иначе выгрузка шести таблиц
    # ClickHouse и двух Postgres отрабатывает минуты, и только финальный
    # tar падает «No such file or directory» — вся работа выброшена.
    # Реальный случай: каталог на внешнем диске не был создан перед
    # переустановкой системы, когда слепок и был нужнее всего.
    local outdir; outdir=$(dirname "$out")
    mkdir -p "$outdir" 2>/dev/null || true
    if [ ! -d "$outdir" ] || [ ! -w "$outdir" ]; then
        echo "ОШИБКА: каталог '$outdir' недоступен для записи." >&2
        echo "  Внешний диск может быть не примонтирован в WSL:" >&2
        echo "    ls /mnt/            # какие диски видны" >&2
        echo "    sudo mkdir -p /mnt/d && sudo mount -t drvfs D: /mnt/d" >&2
        exit 1
    fi
    # Пустой файл в цели: ловим переполненный диск и права до того, как
    # потратим минуты на дампы.
    if ! : >"$out" 2>/dev/null; then
        echo "ОШИБКА: не могу создать '$out' (нет прав или диск полон)" >&2
        exit 1
    fi

    local dir; dir=$(mktemp -d)
    trap "rm -rf '$dir'" EXIT

    local tables=("${REPLACING_TABLES[@]}")
    [ "${SKIP_RAW:-}" = "1" ] || tables+=("${RAW_TABLES[@]}")

    for t in "${tables[@]}"; do
        echo ">> CH $t"
        chq "SELECT * FROM manta.$t FORMAT Native" | gzip >"$dir/$t.native.gz"
    done

    for t in "${PG_TABLES[@]}"; do
        echo ">> PG $t"
        pg_columns "$t" >"$dir/$t.cols"
        pgq -c "\\copy $t TO STDOUT CSV" | gzip >"$dir/$t.csv.gz"
    done

    {
        echo "{\"exported_at\": \"$(date -u +%FT%TZ)\","
        echo " \"matches_in_mart\": $(chq 'SELECT count(DISTINCT match_id) FROM manta.MatchTimelineFeatures FINAL'),"
        # collected/reports — прежние имена полей: их читают учения по
        # восстановлению из архивов, снятых до спринта 156.
        echo " \"collected\": $(pgq -t -A -c 'SELECT count(*) FROM collectedmatches'),"
        echo " \"reports\": $(pgq -t -A -c 'SELECT count(*) FROM matchreports'),"
        for t in "${PG_TABLES[@]}"; do
            echo " \"pg_$t\": $(pgq -t -A -c "SELECT count(*) FROM $t"),"
        done
        echo " \"schema\": 156}"
    } >"$dir/meta.json"

    # Manifest создаётся последним и описывает каждый payload-файл. Сам
    # себя он не включает: иначе контрольная сумма зависела бы от себя.
    (
        cd "$dir"
        for f in ./*; do
            [ -f "$f" ] || continue
            [ "$f" = "./$MANIFEST" ] && continue
            sha256sum "$f"
        done
    ) >"$dir/$MANIFEST"

    tar -cf "$out" -C "$dir" .
    echo
    echo "готово: $out ($(du -h "$out" | cut -f1))"
    cat "$dir/meta.json"
}

# Пустой дамп таблицы — норма, а не ошибка: слепок мог сниматься до
# появления таблицы (архив от 2026-07-27 не содержал MatchDraft/MatchEvents,
# их наполнил трек F позже). ClickHouse на пустом Native отвечает
# NO_DATA_TO_INSERT, и без этой проверки одна пустая таблица роняла ВЕСЬ
# импорт — данные, уже влитые до неё, оставались, остальные терялись.
is_empty_dump() {
    [ "$(gunzip -c "$1" | head -c 1 | wc -c)" -eq 0 ]
}

import_dataset() {
    local in="${1:?путь к архиву}"
    [ -f "$in" ] || { echo "ОШИБКА: слепок '$in' не найден" >&2; return 1; }
    verify_snapshot "$in" || return 1
    local dir; dir=$(mktemp -d)
    trap "rm -rf '$dir'" EXIT
    # Структура и тип каждого member уже проверены выше. Флаги не дают
    # архиву навязать владельца или опасно широкие права принимающей машине.
    tar --no-same-owner --no-same-permissions -xf "$in" -C "$dir"
    echo ">> архив: $(cat "$dir/meta.json" 2>/dev/null || echo 'без meta.json')"

    for t in "${REPLACING_TABLES[@]}"; do
        [ -f "$dir/$t.native.gz" ] || continue
        if is_empty_dump "$dir/$t.native.gz"; then
            echo ">> CH $t — в архиве пусто, пропуск"
            continue
        fi
        echo ">> CH $t (ReplacingMergeTree — вставка как есть)"
        gunzip -c "$dir/$t.native.gz" |
            docker exec -i "$CH" clickhouse-client --user "$CH_USER" --password "$CH_PASS" \
                -q "INSERT INTO manta.$t FORMAT Native"
    done

    for t in "${RAW_TABLES[@]}"; do
        [ -f "$dir/$t.native.gz" ] || continue
        if is_empty_dump "$dir/$t.native.gz"; then
            echo ">> CH $t — в архиве пусто, пропуск"
            continue
        fi
        echo ">> CH $t (MergeTree — только новые match_id через staging)"
        chq "DROP TABLE IF EXISTS manta.${t}_import"
        chq "CREATE TABLE manta.${t}_import AS manta.$t"
        gunzip -c "$dir/$t.native.gz" |
            docker exec -i "$CH" clickhouse-client --user "$CH_USER" --password "$CH_PASS" \
                -q "INSERT INTO manta.${t}_import FORMAT Native"
        chq "INSERT INTO manta.$t SELECT * FROM manta.${t}_import
             WHERE match_id NOT IN (SELECT DISTINCT match_id FROM manta.$t)"
        chq "DROP TABLE manta.${t}_import"
    done

    # COPY FROM STDIN: SQL и CSV-данные идут одним потоком (как pg_dump),
    # конец данных — строка «\.».
    for t in "${PG_TABLES[@]}"; do
        # Таблицы может не быть в архиве — он мог быть снят до того, как
        # её внесли в перенос. Это норма, а не ошибка.
        [ -f "$dir/$t.csv.gz" ] || { echo ">> PG $t — в архиве нет, пропуск"; continue; }
        if is_empty_dump "$dir/$t.csv.gz"; then
            echo ">> PG $t — в архиве пусто, пропуск"
            continue
        fi

        local cols copy_cols merge schema_cols fields pre=""
        cols=$(cat "$dir/$t.cols" 2>/dev/null || true)
        if [ -z "$cols" ]; then
            # Архив снят до спринта 156 и списка колонок не несёт —
            # считаем поля в самом CSV. Именно ради этого случая обмен и
            # чинится: слепки домашней машины лежат в старом формате, и
            # «обновите обе машины и снимите заново» — не ответ, когда
            # переносить нужно как раз потому, что одна из машин чистая.
            schema_cols=$(pg_columns "$t")
            fields=$(csv_field_count "$dir/$t.csv.gz")
            if [ -n "$fields" ] && cols=$(archive_columns "$schema_cols" "$fields")
            then
                echo ">> PG $t — архив без списка колонок, в CSV их $fields"
            else
                # Полей больше, чем в схеме, или CSV не разобрался. Это не
                # то, что можно домыслить: скажем прямо и попробуем как
                # есть — падение будет громким и с понятной причиной.
                echo ">> PG $t — архив без списка колонок, и число полей"
                echo "   не сошлось со схемой; пробую как есть"
                cols="$schema_cols"
            fi
        fi

        # Колонки АРХИВА (для COPY) и колонки ВСТАВКИ — разные списки:
        # чего в архиве не было, то восстанавливается уже в базе.
        copy_cols="$cols"
        pre=$(pg_backfill_sql "$t" "$cols" "${t}_import")
        cols=$(pg_insert_cols "$t" "$cols")

        # Отдельным присваиванием, а не подстановкой внутри echo: там
        # ошибка «нет правила слияния» ушла бы в stderr, а в поток SQL
        # попала бы пустая строка — то есть INSERT без ON CONFLICT,
        # падающий на первом же дубликате уже посреди импорта.
        merge=$(pg_merge_sql "$t" "$cols")

        echo ">> PG $t"
        {
            echo "CREATE TEMP TABLE ${t}_import (LIKE $t INCLUDING ALL);"
            echo "COPY ${t}_import ($copy_cols) FROM STDIN CSV;"
            gunzip -c "$dir/$t.csv.gz"
            echo "\\."
            [ -z "$pre" ] || echo "$pre"
            echo "INSERT INTO $t ($cols) SELECT $cols FROM ${t}_import $merge;"
        } | pgq
    done

    echo
    echo ">> итог"
    echo "   матчей в витрине: $(chq 'SELECT count(DISTINCT match_id) FROM manta.MatchTimelineFeatures FINAL')"
    for t in "${PG_TABLES[@]}"; do
        printf '   %-18s %s\n' "$t:" "$(pgq -t -A -c "SELECT count(*) FROM $t")"
    done
}

case "${1:-}" in
    export) shift; export_dataset "$@";;
    verify) shift; verify_snapshot "${1:?путь к архиву}" && echo "слепок проверен: $1";;
    import) shift; import_dataset "$@";;
    *) usage;;
esac
