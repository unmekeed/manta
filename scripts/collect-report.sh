#!/usr/bin/env bash
# make collect-report — ОДНА команда, отвечающая на вопрос «почему матчей
# стало меньше» и «какие фичи реально заполнены».
#
# Мотив (2026-08-03): за сутки собралось 1000+ матчей, за половину
# следующих — 190. `make doctor` при этом ЗДОРОВ: он отвечает на вопрос
# «конвейер жив?», а не «с какой скоростью и что именно он приносит».
# Между «жив» и «работает в полную силу» помещается весь список причин
# падения темпа: выбранная квота OpenDota, почасовой лимит STRATZ,
# протухший токен, фильтр патча после выхода нового, вставший курсор,
# дедуп на пересечении источников, шардирование между машинами.
#
# Скрипт НИЧЕГО не чинит и не меняет — только читает. Он намеренно
# многословен: это диагностический дамп для чтения человеком, а не
# health-check с кодом возврата (для второго есть doctor.sh). Код
# возврата всегда 0, кроме случая, когда недоступна сама ClickHouse.
#
#   ./scripts/collect-report.sh              # всё
#   ./scripts/collect-report.sh rate         # только темп сбора
#   ./scripts/collect-report.sh features     # только покрытие фич
#   HOURS=72 ./scripts/collect-report.sh     # окно почасовых таблиц
set -uo pipefail
cd "$(dirname "$0")/.."

# Тот же env-файл, что читают dev-recover.sh и doctor.sh: без него не
# видны ни STRATZ_API_TOKEN (а значит, и настроенность второго
# источника), ни шард машины — а именно шард объясняет, почему матчей
# ровно вдвое меньше ожидаемого.
TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
if [ -f "$TRAIN_ENV" ]; then
    set -a; . "$TRAIN_ENV"; set +a
fi

CH_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
CH_DB="${CLICKHOUSE_DB:-manta}"
CH_AUTH=(-H "X-ClickHouse-User: ${CLICKHOUSE_USER:-dota}"
         -H "X-ClickHouse-Key: ${CLICKHOUSE_PASSWORD:-dota_dev_password}")
LOG_DIR="${MANTA_LOG_DIR:-$HOME/manta-logs}"
HOURS="${HOURS:-48}"
SECTION="${1:-all}"

export PGPASSWORD="${PGPASSWORD:-dota_dev_password}"
PG=(psql -h "${POSTGRES_HOST:-localhost}" -U "${POSTGRES_USER:-dota}"
    -d "${POSTGRES_DB:-manta}")

hdr() { printf '\n\033[1m══ %s\033[0m\n' "$*"; }
sub() { printf '\n\033[36m-- %s\033[0m\n' "$*"; }
note() { printf '   \033[90m%s\033[0m\n' "$*"; }

# ClickHouse. Ошибку печатаем, но не падаем: половина отчёта лучше нуля.
ch() {
    local out
    out=$(echo "$1" | curl -s --max-time 60 "$CH_URL/?database=$CH_DB" \
              "${CH_AUTH[@]}" --data-binary @- 2>&1)
    if [ -z "$out" ]; then
        echo "   (пусто)"
    else
        echo "$out"
    fi
}
pg() { "${PG[@]}" -c "$1" 2>&1 | sed 's/^/   /'; }

want() { [ "$SECTION" = "all" ] || [ "$SECTION" = "$1" ]; }

if ! curl -s --max-time 5 "$CH_URL/ping" | grep -q Ok; then
    echo "ClickHouse недоступен ($CH_URL) — сначала make recover" >&2
    exit 1
fi

# ── 0. Кто мы ────────────────────────────────────────────────────────────────
hdr "МАШИНА И КОНФИГУРАЦИЯ СБОРА"
printf '   host=%s   UTC=%s   local=%s\n' "$(hostname)" \
    "$(date -u '+%F %T')" "$(date '+%F %T %Z')"
printf '   шард: COLLECTOR_SHARD_ID=%s из COLLECTOR_SHARD_COUNT=%s\n' \
    "${COLLECTOR_SHARD_ID:-0}" "${COLLECTOR_SHARD_COUNT:-1}"
if [ "${COLLECTOR_SHARD_COUNT:-1}" != "1" ]; then
    note "матчи делятся между машинами по match_id % COUNT — эта машина"
    note "видит ~1/${COLLECTOR_SHARD_COUNT} потока, и это НОРМА, а не потеря"
fi
printf '   лимиты цикла: TIMELINE_LIMIT=%s PRO_TIMELINE_LIMIT=%s STRATZ_LIMIT=%s STRATZ_PRO_LIMIT=%s OPENDOTA_LIMIT=%s\n' \
    "${TIMELINE_LIMIT:-10}" "${PRO_TIMELINE_LIMIT:-5}" \
    "${STRATZ_LIMIT:-40}" "${STRATZ_PRO_LIMIT:-10}" "${OPENDOTA_LIMIT:-1}"
printf '   интервалы, с: TIMELINE=%s PRO_TIMELINE=%s STRATZ=%s STRATZ_PRO=%s\n' \
    "${TIMELINE_INTERVAL:-1800}" "${PRO_TIMELINE_INTERVAL:-3600}" \
    "${STRATZ_INTERVAL:-1800}" "${STRATZ_PRO_INTERVAL:-3600}"
printf '   STRATZ_API_TOKEN: %s   OPENDOTA_API_KEY: %s\n' \
    "$([ -n "${STRATZ_API_TOKEN:-}" ] && echo "задан (${#STRATZ_API_TOKEN} симв.)" || echo "НЕТ — источник stratz выключен")" \
    "$([ -n "${OPENDOTA_API_KEY:-}" ] && echo "задан" || echo "нет (анонимный тариф ~2000/сут)")"
printf '   фильтр патча: OPENDOTA_MIN_PATCH=%s STRATZ_MIN_PATCH=%s PATCH_LAG=%s\n' \
    "${OPENDOTA_MIN_PATCH:-<не задан>}" "${STRATZ_MIN_PATCH:-<не задан>}" \
    "${PATCH_LAG:-1}"

# ── 1. Темп сбора ────────────────────────────────────────────────────────────
if want rate; then
hdr "ТЕМП СБОРА — сколько матчей и откуда"

sub "Матчей по источникам за сутки (Postgres CollectedMatches, UTC)"
pg "SELECT source_name,
           count(*) FILTER (WHERE collected_at > now() - interval '1 hour')  AS \"1ч\",
           count(*) FILTER (WHERE collected_at > now() - interval '6 hours') AS \"6ч\",
           count(*) FILTER (WHERE collected_at > now() - interval '24 hours') AS \"24ч\",
           count(*) FILTER (WHERE collected_at > now() - interval '48 hours'
                              AND collected_at <= now() - interval '24 hours') AS \"пред.сутки\",
           count(*) AS \"всего\",
           to_char(max(collected_at) AT TIME ZONE 'UTC', 'MM-DD HH24:MI') AS \"последний\"
      FROM CollectedMatches GROUP BY source_name ORDER BY 4 DESC;"
note "«24ч» против «пред.сутки» — главная строка отчёта: источник, у"
note "которого упало именно здесь, и есть причина падения темпа."

sub "Почасовой профиль за ${HOURS}ч (UTC; 0 в свежих часах = источник встал)"
pg "SELECT to_char(date_trunc('hour', collected_at AT TIME ZONE 'UTC'), 'MM-DD HH24') AS \"час\",
           count(*) FILTER (WHERE source_name = 'stratz_timeline')      AS \"stratz\",
           count(*) FILTER (WHERE source_name = 'stratz_timeline_pro')  AS \"stratz-pro\",
           count(*) FILTER (WHERE source_name = 'opendota_timeline')    AS \"od-json\",
           count(*) FILTER (WHERE source_name = 'opendota_timeline_pro') AS \"od-pro\",
           count(*) FILTER (WHERE source_name IN ('opendota','opendota_public')) AS \"реплей\",
           count(*) AS \"итого\"
      FROM CollectedMatches
     WHERE collected_at > now() - interval '$HOURS hours'
     GROUP BY 1 ORDER BY 1;"

sub "Курсоры источников — кто вообще делал попытку"
pg "SELECT source_name, cursor_value,
           to_char(updated_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI') AS \"обновлён (UTC)\",
           round(extract(epoch FROM now() - updated_at) / 60) AS \"минут назад\"
      FROM CollectorCursor ORDER BY updated_at DESC;"
note "Курсор двигается ТОЛЬКО при успешно собранном матче. Свежий курсор"
note "при нулевом притоке в таблице выше невозможен; старый курсор при"
note "живом процессе означает, что все кандидаты отсеиваются (см. логи)."

sub "Витрина ClickHouse: приток строк по часам за ${HOURS}ч"
ch "SELECT toStartOfHour(computed_at) AS hour,
           uniqExactIf(match_id, feature_version = 'stratz-graphql@1')  AS stratz,
           uniqExactIf(match_id, feature_version = 'opendota-json@3')   AS od_json,
           uniqExactIf(match_id, feature_version NOT IN
               ('stratz-graphql@1', 'opendota-json@3'))                 AS replay,
           uniqExact(match_id) AS total
      FROM MatchTimelineFeatures
     WHERE computed_at > now() - INTERVAL $HOURS HOUR
     GROUP BY hour ORDER BY hour
     FORMAT PrettyCompactMonoBlock" | sed 's/^/   /'
note "Витрина считает по computed_at и включает ПЕРЕсчёты того же матча;"
note "CollectedMatches выше — по первому сбору. Расхождение нормально."
fi

# ── 2. Квоты и лимиты ────────────────────────────────────────────────────────
if want rate || want quota; then
hdr "КВОТЫ ВНЕШНИХ API — первая подозреваемая при падении темпа"

sub "OpenDota (лимит на IP, сброс 00:00 UTC)"
od=$(curl -sI --max-time 10 https://api.opendota.com/api/health | tr -d '\r')
if [ -z "$od" ]; then
    echo "   OpenDota не ответил — сеть или сам сервис"
else
    echo "$od" | awk -F': ' 'tolower($1) ~ /^x-rate-limit/ {printf "   %s: %s\n", $1, $2}'
    note "до сброса суточной: $(( (86400 - ($(date -u +%s) % 86400)) / 60 )) мин"
fi

if [ -n "${STRATZ_API_TOKEN:-}" ]; then
    sub "STRATZ (лимит на ТОКЕН, не на IP: 20/с, 250/мин, 2000/ч, 10000/сут)"
    st=$(curl -s -o /dev/null -D - --max-time 15 \
             -H "Authorization: Bearer $STRATZ_API_TOKEN" \
             -H "Content-Type: application/json" \
             -H "User-Agent: STRATZ_API" \
             -d '{"query":"{__typename}"}' \
             https://api.stratz.com/graphql | tr -d '\r')
    code=$(head -1 <<<"$st")
    printf '   ответ: %s\n' "$code"
    echo "$st" | awk -F': ' 'tolower($1) ~ /ratelimit/ {printf "   %s: %s\n", $1, $2}'
    case "$code" in
        *401*|*403*) note "токен недействителен — обновить STRATZ_API_TOKEN в $TRAIN_ENV" ;;
        *429*)       note "лимит выбран прямо сейчас — снижать STRATZ_LIMIT или интервал" ;;
    esac
    note "ВАЖНО: лимит привязан к токену. Один токен на две машины = общая"
    note "квота на двоих, и вторая машина получает 429 при «целой» первой."
fi
fi

# ── 3. Логи коллекторов ──────────────────────────────────────────────────────
if want rate || want logs; then
hdr "ЛОГИ КОЛЛЕКТОРОВ ($LOG_DIR)"
# stratz-pro.log читаем, хотя источник со спринта 93 не запускается:
# в нём осталась история, по которой видно, когда и почему он встал.
for f in stratz stratz-pro timeline timeline-pro collector pro-collector; do
    p="$LOG_DIR/$f.log"
    [ -f "$p" ] || { printf '\n   %-14s лога нет (коллектор ни разу не запускался)\n' "$f"; continue; }
    age=$(( ($(date +%s) - $(stat -c %Y "$p")) / 60 ))
    sub "$f.log — последняя запись $age мин назад, $(du -h "$p" | cut -f1)"
    if [ "$age" -gt 120 ]; then
        note "лог молчит >2ч: процесс убит, спит по 429 или завис на запросе"
    fi

    # Успешные циклы по часам: видно и частоту циклов, и их выхлоп.
    grep -h 'cycle done' "$p" 2>/dev/null | tail -500 |
        sed -nE 's/^\{"time":"([0-9-]+) ([0-9]{2}):[^"]*".*processed=([0-9]+).*/\1 \2 \3/p' |
        awk '{k = $1 " " $2; c[k]++; s[k] += $3}
             END {for (k in c) printf "   %s  циклов %2d  матчей %4d\n", k, c[k], s[k]}' |
        sort | tail -12
    note "время в логах ЛОКАЛЬНОЕ, в таблицах Postgres выше — UTC"

    # Причины отсева. Без них в логе виден только итог «собрано N», и
    # непонятно, упёрлись мы в лимит, в дедуп или в фильтр качества —
    # лечатся эти три случая по-разному.
    grep -h 'цикл STRATZ' "$p" 2>/dev/null | tail -3 |
        sed -nE 's/^\{"time":"([^,"]*)[^}]*"msg":"([^"]*)".*/   \1  \2/p'
    bud=$(grep -hc 'бюджет detail-вызовов' "$p" 2>/dev/null)
    [ "${bud:-0}" -gt 0 ] && note "циклов, упёршихся в detail_budget: $bud (лимит цикла мал → поднять TIMELINE_LIMIT)"

    # «карта патчей ПУСТА» попала сюда по итогам разбора 2026-08-03: у
    # 43% датасета стоял patch=0, и в отчёте это было видно только
    # косвенно — колонками patch_min/patch_max в разбивке ниже.
    for pat in '429' 'токен недействителен' 'квота OpenDota исчерпана' \
               'минутный лимит' 'PermanentDownloadError' 'неизвестный формат' \
               'карта патчей' 'не разобрать ответ' 'бюджет вызовов' \
               'соединение умерло' 'ERROR'; do
        n=$(grep -hc -- "$pat" "$p" 2>/dev/null)
        [ "${n:-0}" -gt 0 ] && printf '   %-28s %s раз(а), последний: %s\n' \
            "«$pat»" "$n" "$(grep -h -- "$pat" "$p" | tail -1 | \
                             sed -nE 's/.*"time":"([^,"]*).*/\1/p')"
    done
    printf '   последняя строка: %s\n' "$(tail -1 "$p" | cut -c1-220)"
done
fi

# ── 4. Процессы и счётчики ───────────────────────────────────────────────────
if want rate || want procs; then
hdr "ПРОЦЕССЫ И СЧЁТЧИКИ PROMETHEUS"
check() {  # имя, шаблон pgrep, порт метрик
    if pgrep -f "$2" >/dev/null; then
        m=$(curl -s --max-time 5 "http://localhost:$3/metrics" 2>/dev/null)
        if [ -z "$m" ]; then
            printf '   %-16s up   :%s метрики НЕ отдаёт (порт занят? см. лог)\n' "$1" "$3"
            return
        fi
        val() { awk -v k="$1" '$1 == k {printf "%d", $2}' <<<"$m"; }
        printf '   %-16s up   собрано=%s  циклов упало=%s  429=%s\n' "$1" \
            "$(val matches_collected_total)" \
            "$(val collector_cycles_failed_total)" \
            "$(val opendota_rate_limited_total)"
    else
        printf '   \033[33m%-16s DOWN\033[0m — make recover\n' "$1"
    fi
}
check stratz       "collector --source stratz-timeline --interval"     9115
check od-timeline  "collector --source opendota-timeline --interval"   9108
check od-pro       "collector --source opendota-timeline-pro"          9110
check od-public    "collector --source opendota-public"                9105
check pro-replay   "collector --source opendota --interval"            9109
note "счётчики обнуляются при перезапуске процесса — сравнивать их можно"
note "только с предыдущим прогоном этого же отчёта, не между машинами"
fi

# ── 5. Датасет и покрытие фич ────────────────────────────────────────────────
if want features; then
hdr "ДАТАСЕТ: ОБЪЁМ И РАЗБИВКА"
ch "SELECT feature_version, tier,
           uniqExact(match_id) AS matches,
           min(patch) AS patch_min, max(patch) AS patch_max,
           round(avg(radiant_win), 3) AS radiant_wr,
           max(computed_at) AS last_write
      FROM MatchTimelineFeatures FINAL
     GROUP BY feature_version, tier ORDER BY matches DESC
     FORMAT PrettyCompactMonoBlock" | sed 's/^/   /'
note "radiant_wr здесь по СТРОКАМ, не по матчам — оценка приора грубая;"
note "точный аудит смещения: make ml-audit"
note "patch_max=0 у источника = карта патчей не построилась (см. лог выше):"
note "даунвейт старых патчей (A9) для этих матчей не работает"

hdr "ПОКРЫТИЕ ФИЧ — сколько матчей реально имеют значение, а не NaN"
# Список Float-колонок берём из системной таблицы, а не хардкодим: любая
# новая миграция трека попадает в отчёт сама. Целочисленные колонки
# (networth_diff, kills_*) NaN хранить не могут и заполнены всегда.
cols=$(ch "SELECT name FROM system.columns
            WHERE database = '$CH_DB' AND table = 'MatchTimelineFeatures'
              AND type LIKE 'Float%' ORDER BY position FORMAT TabSeparated")
total=$(ch "SELECT uniqExact(match_id) FROM MatchTimelineFeatures FINAL FORMAT TabSeparated")
printf '   всего матчей в витрине: %s\n\n' "$total"
# Заголовок выровнен литералом, а не printf: bash считает ширину %-22s в
# БАЙТАХ, и кириллица (2 байта на букву) съезжает относительно данных.
echo   "   фича                       матчей        %      за 24ч"
printf '   %-22s %10s %8s %11s\n' "----------------------" "----------" "--------" "-----------"
for c in $cols; do
    row=$(ch "SELECT uniqExactIf(match_id, NOT isNaN($c)),
                     round(100 * uniqExactIf(match_id, NOT isNaN($c))
                             / greatest(uniqExact(match_id), 1), 1),
                     uniqExactIf(match_id, NOT isNaN($c)
                                 AND computed_at > now() - INTERVAL 24 HOUR)
                FROM MatchTimelineFeatures FINAL FORMAT TabSeparated")
    printf '   %-22s %10s %8s %11s\n' "$c" $row
done
note "0% у фич трека F и A14 на JSON-матчах — ОЖИДАЕМО: их источник —"
note "реплей (position_advance, alive_diff, local_manpower_diff, spread_diff)"
note "или сырой JSON OpenDota (roshan_diff, wards, items). У STRATZ их нет."
note "Тревожно другое: колонка, у которой «за 24ч» = 0 при ненулевом «матчей»,"
note "— значит, поставщик этой фичи перестал работать СЕГОДНЯ."

sub "Матчей с полным набором сигналов трека F (порог ревизии — 2000)"
ch "SELECT uniqExact(match_id) AS with_signals
      FROM MatchTimelineFeatures FINAL
     WHERE NOT isNaN(roshan_diff) AND NOT isNaN(obs_wards_diff)
       AND NOT isNaN(item_value_diff)
     FORMAT TabSeparated" | sed 's/^/   /'

hdr "СОПУТСТВУЮЩИЕ ТАБЛИЦЫ"
for t in MatchDraft MatchEvents MatchFights ReplayEvents PositionSnapshots EconomyTimeline; do
    n=$(ch "SELECT uniqExact(match_id) FROM $t FORMAT TabSeparated" 2>/dev/null)
    mt=$(ch "SELECT ifNull(toString(max(modification_time)), '-') FROM system.parts
              WHERE database = '$CH_DB' AND table = '$t' AND active
              FORMAT TabSeparated" 2>/dev/null)
    printf '   %-20s матчей %-8s последняя запись %s\n' "$t" "${n:-?}" "${mt:--}"
done
note "ReplayEvents живёт 14 дней (TTL, миграция 007) — падение числа матчей"
note "там нормально; MatchFights без TTL и обязана только расти."

sub "Драки: распределение по числу участников (MatchFights)"
ch "SELECT radiant_participants + dire_participants AS participants,
           count() AS fights
      FROM MatchFights FINAL GROUP BY participants ORDER BY participants
      FORMAT PrettyCompactMonoBlock" | sed 's/^/   /'
fi

hdr "КАК ЧИТАТЬ ЭТОТ ОТЧЁТ"
cat <<'EOF'
   Падение темпа сбора почти всегда одна из шести причин. Проверять
   строго в этом порядке — сверху дешевле и вероятнее:

   1. Квота/лимит внешнего API. Признак: в разделе КВОТЫ remaining-day
      около нуля либо STRATZ отвечает 429; в логах растёт счётчик «429».
      Лечение: дождаться сброса; разнести интервалы; свой токен на
      каждую машину (лимит STRATZ — на токен, не на IP).
   2. Токен протух. Признак: 401/403 от STRATZ, счётчик «токен
      недействителен» в логе. Лечение: обновить в env-файле, make recover.
   3. Вышел новый патч. Признак: приток упал до нуля У ВСЕХ источников
      сразу, в логе «актуальный патч: id=…», кандидаты уходят в «фильтр».
      Лечение: PATCH_LAG (по умолчанию 1 — принимаем и предыдущий).
   4. Процесс умер. Признак: DOWN в разделе ПРОЦЕССЫ, лог молчит часами.
      Лечение: make recover (идемпотентно).
   5. Кандидаты кончились, а не отсеялись. Признак: в строке «цикл
      STRATZ» велики «дубликат» и «чужой шард», «собрано» мало. Это
      потолок источника: /parsedMatches отдаёт окно из ~100 матчей, и на
      двух машинах каждая берёт свою половину. Лечение — не крутить
      лимиты, а добавлять источник.
   5а. Источник душит сам себя кэшем отказов. Признак: «кэш отказов»
      РАСТЁТ от цикла к циклу, «собрано» падает, а квота цела. Так
      выглядел дефект до спринта 87: временная причина («нет данных» —
      STRATZ ещё не распарсил свежий матч) писалась в ПОСТОЯННЫЙ кэш, и
      матч не пробовался больше никогда. Теперь такие матчи считают
      попытки и видны в логе как «ждут парсинга»; в постоянный отказ они
      переезжают только после STRATZ_RETRY_ATTEMPTS (3). Если «ждут
      парсинга» велико и не убывает — STRATZ отстаёт от листинга
      OpenDota; лечится интервалом коллектора, а не лимитом.
   6. Ничего не сломано. Сравните «24ч» с «пред.сутки» ПО ИСТОЧНИКАМ:
      если упал ровно один, причина в нём; если все поровну — смотрите
      на время суток. Прайм-тайм даёт кратно больше сыгранных матчей,
      чем ночь, и половина суток НЕ равна половине суточного улова.
EOF
