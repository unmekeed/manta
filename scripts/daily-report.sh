#!/usr/bin/env bash
# Ежедневный снимок состояния проекта одним файлом (спринт 112).
#
#   ./scripts/daily-report.sh              # снять отчёт за сегодня
#   MANTA_REPORT_DIR=/mnt/d/manta-reports ./scripts/daily-report.sh
#
# Зачем. Диагностические команды (doctor, collect-report, ml-audit,
# ml-status) отвечают на вопрос «что сейчас». Но самые дорогие наши
# ошибки были не мгновенными, а ПОСТЕПЕННЫМИ: темп сбора сползал неделю,
# про-эталон замёрз на двое суток, покрытие фич падало от патча к патчу.
# Всё это видно только в сравнении с прошлым, а прошлого не оставалось —
# вывод команд жил в терминале до следующей прокрутки.
#
# Формат: ОДИН файл на день, `manta-YYYY-MM-DD.log`, обычный текст.
# Не JSON: файл читает человек, а diff двух дней текстовым `diff`
# осмысленнее, чем сравнение деревьев.
#
# Ротация: REPORT_KEEP_DAYS (по умолчанию 30) — старые удаляются ПОСЛЕ
# успешной записи нового, как в backup.sh. Сбой не должен оставлять без
# истории.
#
# set -uo pipefail БЕЗ -e — намеренно, ровно как в collect-report.sh:
# отчёт обязан дойти до конца, даже если ClickHouse лежит или модель не
# обучена. Диагностику смотрят как раз тогда, когда что-то сломано, и
# оборваться на первом же сломанном разделе значит не сказать ничего.
set -uo pipefail
cd "$(dirname "$0")/.."

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

REPORT_DIR="${MANTA_REPORT_DIR:-$HOME/manta-reports}"
KEEP_DAYS="${REPORT_KEEP_DAYS:-30}"
mkdir -p "$REPORT_DIR"

# Дата ЛОКАЛЬНАЯ, а не UTC: файл ищет человек по своему календарю, и
# «отчёт за 5 августа», созданный в 03:00 UTC 6-го, сбивал бы с толку.
day=$(date +%F)
out="$REPORT_DIR/manta-$day.log"

section() {
    printf '\n%s\n== %s\n%s\n' \
        "════════════════════════════════════════════════════════════" \
        "$1" \
        "════════════════════════════════════════════════════════════"
}

# Каждый раздел с таймаутом: зависший вызов внешнего API не должен
# держать отчёт вечно (планировщик запустит следующий поверх).
run() {   # заголовок  команда...
    local title="$1"; shift
    section "$title"
    timeout "${SECTION_TIMEOUT_S:-600}" "$@" 2>&1
    local rc=$?
    [ "$rc" -eq 124 ] && echo "!! раздел не уложился в таймаут"
    [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && echo "!! раздел завершился с кодом $rc"
    return 0
}

{
    echo "Manta — ежедневный отчёт за $day"
    echo "машина: $(hostname)   собрано: $(date '+%F %T %Z')"
    # Тему коммита НЕ обрезаем. `head -c 60` резал бы по БАЙТАМ и рубил
    # кириллический символ пополам — файл переставал быть валидным UTF-8
    # целиком, а сообщения у нас на русском. Обрезка тут косметическая,
    # цена ошибки — нечитаемый отчёт.
    echo "коммит: $(git rev-parse --short HEAD 2>/dev/null || echo '?')" \
         "($(git log -1 --format=%s 2>/dev/null))"
    echo "шард: COLLECTOR_SHARD_ID=${COLLECTOR_SHARD_ID:-0}" \
         "COLLECTOR_SHARD_COUNT=${COLLECTOR_SHARD_COUNT:-1}"

    run "doctor — конвейер жив?"            ./scripts/doctor.sh
    run "collect-report — темп и покрытие"  ./scripts/collect-report.sh
    run "ml-status — что в production"      make ml-status
    run "ml-audit — качество датасета"      make ml-audit

    section "ИТОГ"
    echo "Файлы отчётов: $REPORT_DIR (хранятся $KEEP_DAYS дней)."
    echo "Сравнить с прошлым днём:"
    echo "  diff $REPORT_DIR/manta-$(date -d '1 day ago' +%F 2>/dev/null).log $out"
} >"$out" 2>&1

if [ ! -s "$out" ]; then
    echo "ОТЧЁТ ПУСТ: $out" >&2
    exit 1
fi

# Ротация — только после успешной записи.
removed=$(find "$REPORT_DIR" -maxdepth 1 -name 'manta-*.log' \
    -mtime "+$KEEP_DAYS" -print -delete 2>/dev/null | wc -l)
kept=$(find "$REPORT_DIR" -maxdepth 1 -name 'manta-*.log' 2>/dev/null | wc -l)

echo ">> отчёт: $out ($(wc -l <"$out") строк)"
echo ">> в каталоге отчётов: $kept (удалено старых: $removed)"
