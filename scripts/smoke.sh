#!/usr/bin/env bash
# make smoke — прогнать ОДИН матч сквозь конвейер и дождаться отчёта.
#
#     ./scripts/smoke.sh              # ждать до 120 с
#     ./scripts/smoke.sh --timeout 300
#
# ЗАЧЕМ. Всё в этом проекте покрыто модульно, а все аварии сентября
# случились НА СТЫКАХ, и ни одна не могла быть поймана тестом отдельного
# куска — каждый кусок был исправен:
#
#   · топика не было → продюсер терял сообщения молча (трое суток);
#   · коллектор не публиковал событие вовсе (спринт 195);
#   · пересобрали не тот сервис, новый код не исполнялся (спринт 195);
#   · SQL страницы ни разу не выполнялся (спринт 198c);
#   · имя источника писалось двумя способами (спринт 198e).
#
# Пять из семи разобранных дефектов эта проверка заметила бы: у каждого
# наблюдаемое следствие одно — событие ушло, а отчёт не появился.
#
# КАК УСТРОЕНО И ПОЧЕМУ БЕЗ СИНТЕТИКИ. Берётся НАСТОЯЩИЙ свежий матч из
# витрины, по нему публикуется `features.calculated`, и проверяется, что
# отметка времени его отчёта СДВИНУЛАСЬ.
#
# Синтетический матч был бы проще, но опаснее: строки витрины с выдуманным
# match_id попали бы в обучающую выборку. Уборка после себя решает это лишь
# пока уборка срабатывает, а прерванный прогон оставил бы мусор в датасете
# молча — то есть завёл бы ровно ту беду, от которой этот проект лечится
# весь сентябрь.
#
# Настоящий матч ничем не рискует: отчёты перегенерируемы по построению
# (спринт 195), а повторная генерация лишь обновит существующий.
#
# ЧЕГО ЭТА ПРОВЕРКА НЕ ДЕЛАЕТ. Не трогает реплейный путь: у него на входе
# .dem, которого без настоящей закачки взять неоткуда. Проверяется хвост —
# витрина → событие → отчёт → карточка, то есть ровно то, что ломалось.
set -uo pipefail
cd "$(dirname "$0")/.."

TIMEOUT_S=120
[ "${1:-}" = "--timeout" ] && TIMEOUT_S="${2:-120}"

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
if [ -f "$TRAIN_ENV" ]; then
    set -a; . "$TRAIN_ENV"; set +a
fi

CH_URL="${CLICKHOUSE_URL:-http://localhost:8123}"
CH_DB="${CLICKHOUSE_DB:-manta}"
CH_AUTH=(-H "X-ClickHouse-User: ${CLICKHOUSE_USER:-dota}"
         -H "X-ClickHouse-Key: ${CLICKHOUSE_PASSWORD:-dota_dev_password}")
KAFKA_BIN="docker exec manta-kafka-1 /opt/kafka/bin"
TOPIC="features.calculated"

ok()   { printf '   \033[32m OK \033[0m %s\n' "$*"; }
warn() { printf '   \033[33mWARN\033[0m %s\n' "$*"; }
fail() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }

ch() { echo "$1" | curl -s --max-time 15 "$CH_URL/?database=$CH_DB" \
           "${CH_AUTH[@]}" --data-binary @-; }
pg() { docker exec manta-postgres-1 psql -U "${POSTGRES_USER:-dota}" \
           -d "${POSTGRES_DB:-manta}" -tAc "$1" 2>/dev/null | tr -d ' \r'; }

echo "== Сквозной прогон: витрина → событие → отчёт"

# -- матч, на котором гоняем ---------------------------------------------------
#
# Берём свежий, а не первый попавшийся: у старого матча реплей мог уже
# уехать по ретеншену, и часть разбора собралась бы не полностью. Отказ
# был бы настоящим, но НЕ ТЕМ, о котором эта проверка.
match_id=$(ch "SELECT match_id FROM MatchTimelineFeatures
                ORDER BY computed_at DESC LIMIT 1" | tr -d ' \r')
if ! [[ "$match_id" =~ ^[0-9]+$ ]]; then
    fail "витрина пуста или недоступна — сквозной прогон не на чем делать"
    exit 1
fi
ok "матч для прогона: $match_id"

# -- предусловие: топик существует ---------------------------------------------
#
# Проверяется ОТДЕЛЬНО и ДО публикации, потому что при отсутствующем
# топике продюсер молчит (AUTO_CREATE отключён намеренно), и без этой
# строки прогон свалился бы в общий таймаут с диагнозом «отчёт не
# появился» — верным по форме и бесполезным по содержанию.
if ! $KAFKA_BIN/kafka-topics.sh --bootstrap-server localhost:9092 --list \
        2>/dev/null | grep -qx "$TOPIC"; then
    fail "топика $TOPIC нет — событие уйдёт в никуда. Лечение: make topics"
    exit 1
fi
ok "топик $TOPIC на месте"

before=$(pg "SELECT COALESCE(to_char(generated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US'), 'нет')
               FROM MatchReports WHERE match_id = $match_id")
[ -z "$before" ] && before="нет"
echo "   отчёт до прогона: $before"

# -- публикация ----------------------------------------------------------------
#
# Конверт минимальный: report-generator читает из payload только
# match_id. Остальные поля — по схеме Гл. 2.3.3, чтобы событие ничем не
# отличалось от боевого; потребитель, начавший однажды смотреть на
# producer или trace_id, не должен спотыкаться о нашу проверку.
trace=$(date +%s%N)
envelope=$(printf '{"event_id":"smoke-%s","event_type":"%s",' "$trace" "$TOPIC")
envelope+=$(printf '"schema_version":"1.0.0","trace_id":"smoke-%s",' "$trace")
envelope+=$(printf '"occurred_at":"%s",' "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
envelope+=$(printf '"producer":"smoke@1.0.0","partition_key":"match_id:%s",' \
                   "$match_id")
envelope+=$(printf '"payload":{"match_id":%s,"feature_version":"smoke"}}' \
                   "$match_id")

if ! printf '%s\n' "$envelope" | $KAFKA_BIN/kafka-console-producer.sh \
        --bootstrap-server localhost:9092 --topic "$TOPIC" >/dev/null 2>&1; then
    fail "событие не опубликовано — Kafka недоступна"
    exit 1
fi
ok "событие опубликовано"

# -- ожидание ------------------------------------------------------------------
#
# Опрашиваем базу, а не логи: лог говорит, что генератор ПЫТАЛСЯ, а
# вопрос в том, появился ли ОТЧЁТ. Ровно то различие, из-за которого
# «конвейер жив» и «данные свежие» — разные утверждения.
waited=0
while [ "$waited" -lt "$TIMEOUT_S" ]; do
    sleep 5
    waited=$((waited + 5))
    now=$(pg "SELECT COALESCE(to_char(generated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US'), 'нет')
                FROM MatchReports WHERE match_id = $match_id")
    [ -z "$now" ] && now="нет"
    if [ "$now" != "$before" ] && [ "$now" != "нет" ]; then
        ok "отчёт обновлён за ${waited}с: $now"
        # Блок available появился в спринте 195 и говорит сайту, каких
        # разделов у матча не бывает. Его отсутствие означает, что
        # генератор крутит код старше 195-го, — то есть ровно тот отказ,
        # который дважды за неделю выглядел как успешная раскатка.
        has=$(pg "SELECT (analysis ? 'available')::text
                    FROM MatchReports WHERE match_id = $match_id")
        if [ "$has" = "true" ]; then
            ok "разбор содержит блок available"
        else
            fail "в разборе нет блока available — report-generator крутит код до спринта 195"
            exit 1
        fi
        echo
        printf '\033[32m>> КОНВЕЙЕР ЖИВ\033[0m (матч %s, %sс)\n' \
               "$match_id" "$waited"
        exit 0
    fi
done

# -- отказ ---------------------------------------------------------------------
#
# Диагноз не выдумываем, а сужаем: печатаем то, что отличает «событие не
# доехало» от «доехало и упало». Без этого читатель начинает с нуля ровно
# в тот момент, когда у него меньше всего времени.
fail "отчёт не появился за ${TIMEOUT_S}с"
echo
echo "   Что смотреть, по порядку:"
echo "   1. Дошло ли событие до потребителя:"
echo "      $KAFKA_BIN/kafka-consumer-groups.sh --bootstrap-server localhost:9092 \\"
echo "          --describe --group report-generator"
echo "      Ненулевой LAG — событие лежит непрочитанным: генератор стоит."
echo "      Нулевой LAG — событие прочитано, и упало уже внутри."
echo "   2. Что сказал генератор:"
echo "      journalctl CONTAINER_NAME=manta-report-generator-1 --since -10min"
echo "   3. Отвечает ли модель (частая причина — она, а не конвейер):"
echo "      docker logs --tail 30 manta-ml-service-1"
exit 1
