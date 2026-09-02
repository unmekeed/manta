#!/usr/bin/env bash
# Сторож: одно короткое сообщение в Telegram за прогон.
#
#     make heartbeat          # прогнать сейчас
#     make tg-test            # проверить, что канал вообще работает
#
# ЗАЧЕМ. Бэкапы на живой машине встали 5 августа и никто не узнал об этом
# ТРИНАДЦАТЬ ДНЕЙ. Причина не в отсутствии алертов, а в их устройстве:
#
#   1. `backup.sh` шлёт сообщение только на ПЕРВОМ сбое: дальше состояние
#      уже «fail», и повторных писем нет. Пропустил одно — не узнаешь
#      никогда. Это защита от спама, и она же превращает поломку в тишину.
#
#   2. Если скрипт не запустился ВОВСЕ (машина выключена, задача не
#      сработала), сбоя нет — значит и алерта нет. Молчание неотличимо от
#      успеха, а это худший вид отчёта.
#
#   3. `tg()` в backup.sh молча возвращает успех, если токен не задан.
#      Ненастроенные уведомления выглядят как настроенные.
#
# ЧТО ДЕЛАЕТ ЭТОТ СКРИПТ. Шлёт сообщение КАЖДЫЙ прогон — и когда всё
# хорошо, и когда плохо. Это и есть защита от третьего случая: раз
# сообщение ждут ежедневно, его ОТСУТСТВИЕ само становится сигналом.
# Алерт «сломалось» поймать нельзя, если ловить некому; а «сегодня не
# пришло привычное сообщение» замечает человек без всякой автоматики.
#
# На VPS без браузера это единственная телеметрия, которая доходит сама.
# Grafana остаётся для разбирательств — туда ходят через SSH-туннель,
# когда уже знают, что смотреть.
#
# ПОЧЕМУ ПОВЕРХ doctor.sh. Он уже считает свежесть данных, лаг топиков,
# квоту и миграции. Заводить второй источник истины значило бы получить
# два расходящихся мнения о здоровье — и не знать, какому верить.
set -uo pipefail
cd "$(dirname "$0")/.."

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

BACKUP_DIR="${MANTA_BACKUP_DIR:-$HOME/manta-backups}"
# Сколько суток без успешного бэкапа считать бедой. Трое: суточный сбой
# бывает от перезагрузки, три подряд — это уже система.
BACKUP_STALE_DAYS="${BACKUP_STALE_DAYS:-3}"
HOST="${MANTA_HOST_LABEL:-$(hostname)}"

send() {
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        # Громко, а не молча. Ненастроенный канал — это не «уведомления
        # выключены», это «сторож не сторожит», и знать об этом надо
        # сейчас, а не через тринадцать дней.
        echo "ОШИБКА: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы в $TRAIN_ENV" >&2
        echo "Сторож без канала бесполезен: сообщать о поломке будет некуда." >&2
        return 2
    fi
    curl -s --max-time 20 \
        "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d "chat_id=$TELEGRAM_CHAT_ID" -d "parse_mode=HTML" \
        --data-urlencode "text=$1" >/dev/null
}

# -- проверка отсутствия, которой нет в doctor ---------------------------------

backup_age_days() {
    local newest
    newest=$(ls -1t "$BACKUP_DIR"/manta-dataset-*.tar 2>/dev/null | head -1)
    [ -n "$newest" ] || { echo "нет"; return; }
    echo $(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 86400 ))
}

# -- сбор состояния ------------------------------------------------------------

doctor_out=$(./scripts/doctor.sh 2>&1)
doctor_code=$?
# Строки FAIL из doctor: только они идут в сообщение. Полный вывод длинный
# и в мессенджере нечитаем, а список проблем — ровно то, что нужно.
problems=$(printf '%s\n' "$doctor_out" | grep -E '^\s+FAIL' | sed 's/^ *FAIL */• /' || true)

bage=$(backup_age_days)
if [ "$bage" = "нет" ]; then
    bage_text="бэкапов нет"
    problems=$(printf '%s\n• бэкапов нет вовсе' "$problems")
    doctor_code=1
elif [ "$bage" -ge "$BACKUP_STALE_DAYS" ]; then
    bage_text="бэкап $bage сут назад"
    problems=$(printf '%s\n• последний бэкап %s суток назад' "$problems" "$bage")
    doctor_code=1
else
    bage_text="бэкап $bage сут назад"
fi

matches=$(printf '%s\n' "$doctor_out" |
    grep -oE 'матчей [0-9]+' | grep -oE '[0-9]+' | head -1)
quota=$(printf '%s\n' "$doctor_out" |
    grep -oE 'remaining-day=[0-9-]+' | head -1)

# Деньги за месяц (спринт 183) — из отчёта doctor.sh, а НЕ своим
# запросом к базе. Сторож строится поверх доктора и не заводит второго
# мнения о состоянии машины: иначе два места считали бы одно и то же и
# однажды посчитали бы по-разному. Поймано собственным тестом
# `test_built_on_top_of_doctor_not_beside_it`.
money=$(printf '%s\n' "$doctor_out" | grep -oE 'OpenDota за месяц: [^(]+\([0-9]+% потолка\)' | head -1)
money_alert=""
pct=$(printf '%s' "$money" | grep -oE '[0-9]+% потолка' | grep -oE '^[0-9]+')
if [ -n "$pct" ] && [ "$pct" -ge "${OPENDOTA_ALERT_PCT:-80}" ]; then
    # Порог 80%, а не 100%: сообщать о перерасходе, когда он уже
    # случился, поздно — деньги потрачены. На 80% ещё можно решить,
    # поднимать потолок или переждать до первого числа.
    money_alert="• $money"
    problems=$(printf '%s\n%s' "$problems" "$money_alert")
    doctor_code=1
fi

# -- сообщение -----------------------------------------------------------------

if [ "$doctor_code" -eq 0 ]; then
    text="✅ <b>Manta</b> ($HOST): всё в норме
матчей: ${matches:-?}   ${bage_text}
${quota:-квота неизвестна}${money:+
$money}"
else
    text="🔴 <b>Manta</b> ($HOST): есть проблемы
$(printf '%s' "$problems" | sed '/^$/d')

матчей: ${matches:-?}   ${bage_text}${money:+
$money}
лечение: docs/runbooks.md"
fi

printf '%s\n' "$text"
send "$text" || exit 2
exit "$doctor_code"
