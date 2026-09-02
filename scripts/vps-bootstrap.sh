#!/usr/bin/env bash
# Развёртывание Manta на чистом VPS (спринт 143).
#
#     ./scripts/vps-bootstrap.sh              # полная установка
#     ./scripts/vps-bootstrap.sh --check      # только проверить готовность
#     ./scripts/vps-bootstrap.sh --only cron  # только один шаг (список: --only ?)
#
# Скрипт ИДЕМПОТЕНТЕН: его можно гонять повторно после сбоя или обновления
# репозитория, ничего не сломается.
#
# ЧТО ОН ДЕЛАЕТ И В КАКОМ ПОРЯДКЕ
#
#   1. Пароли. Генерирует их ОДИН раз и кладёт в два файла сразу:
#      deployments/.env (его читает docker compose) и ~/manta-train.env
#      (его читают скрипты на хосте — бэкап, миграции, обмен). Файлы
#      разные, пароль обязан быть один: разъехавшись, они дают не отказ, а
#      «пароль неверен» из скрипта при живом контейнере.
#   2. Docker, если его нет.
#   3. Инструменты хоста, которых требуют СОБСТВЕННОЕ расписание и
#      СОБСТВЕННЫЕ инструкции этой установки: make и rclone.
#   4. Фаервол: наружу только SSH. Всё остальное — через SSH-туннель.
#   5. Стек с наложением docker-compose.vps.yml (порты на 127.0.0.1,
#      потолки памяти).
#   6. Миграции.
#   7. Расписание: бэкап, обмен с домашней машиной, сторож.
#
# ЧЕГО ОН НЕ ДЕЛАЕТ. Не трогает уже сгенерированный пароль — см. ниже, это
# самое опасное место всего скрипта. Не НАСТРАИВАЕТ rclone: сам бинарь он
# ставит (без него ночной обмен падал бы каждую ночь), а вот OAuth Google
# Drive требует браузера — конфиг делается дома и переносится. Не заливает
# датасет: это отдельный шаг, см. docs/SETUP-VPS.md.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)

ENV_COMPOSE="$REPO/deployments/.env"
ENV_HOST="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
COMPOSE="docker compose -f deployments/docker-compose.yml -f deployments/docker-compose.vps.yml"
CHECK_ONLY=0

say()  { printf '\n>> %s\n' "$1"; }
ok()   { printf '   OK   %s\n' "$1"; }
warn() { printf '   ВНИМАНИЕ %s\n' "$1"; }
die()  { printf '\nОСТАНОВ: %s\n' "$1" >&2; exit 1; }

# Шаги установки: имя для человека — функция, которая его делает. Список
# ОДИН, из него и порядок прогона, и разбор --only: два перечня шагов
# однажды разошлись бы, и --only тихо пропускал бы существующий шаг.
STEPS=(secrets:setup_secrets
       docker:setup_docker
       tools:setup_host_tools
       keys:setup_gateway_keys
       firewall:setup_firewall
       stack:start_stack
       cron:setup_cron)

# --only: выполнить НАЗВАННЫЕ шаги, а не всё подряд (спринт 176).
#
# ЗАЧЕМ. Полный прогон пересобирает образы по одному — на двухъядерном
# VPS это десятки минут. Когда обновилась одна строка расписания, цена
# «применить правку» оказывалась несоизмерима с правкой, и правку
# откладывали. Так и вышло в спринтах 159–161: три коммита-починки cron
# подряд не доезжали до машины, потому что ради них надо было пересобрать
# весь стек.
#
# Установка, которую нельзя применить частично, применяется реже, чем
# нужно, — а значит машина живёт со старой конфигурацией.
ONLY=""

step_names() { local s; for s in "${STEPS[@]}"; do printf '%s ' "${s%%:*}"; done; }

for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --only)  ONLY="ЖДУ" ;;
        --only=*) ONLY="${arg#--only=}" ;;
        -*) die "неизвестный флаг $arg (есть --check и --only <шаг>)" ;;
        *) if [ "$ONLY" = "ЖДУ" ]; then ONLY="$arg"
           else die "лишний аргумент $arg"; fi ;;
    esac
done
[ "$ONLY" = "ЖДУ" ] && die "--only без имени шага. Доступны: $(step_names)"
if [ "$ONLY" = "?" ]; then
    printf 'шаги установки: %s\n' "$(step_names)"
    exit 0
fi
# Имена сверяются ДО первого действия. Опечатка в шаге иначе прошла бы
# как «ничего не делать» — и выглядело бы это как успешная установка.
if [ -n "$ONLY" ]; then
    for want in ${ONLY//,/ }; do
        case " $(step_names)" in
            *" $want "*) ;;
            *) die "нет шага «$want». Доступны: $(step_names)" ;;
        esac
    done
fi

wanted_step() {
    [ -z "$ONLY" ] && return 0
    case ",$ONLY," in *",$1,"*) return 0 ;; esac
    return 1
}

need_root() {
    [ "$(id -u)" = "0" ] || command -v sudo >/dev/null || \
        die "нужен root или sudo"
}
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo"

# -- 1. пароли -----------------------------------------------------------------

gen_password() {
    # openssl есть на любой Ubuntu; head -c из /dev/urandom — запасной путь.
    if command -v openssl >/dev/null; then
        openssl rand -base64 24 | tr -d '/+=' | cut -c1-24
    else
        tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24
    fi
}

setup_secrets() {
    say "пароли"

    # САМОЕ ОПАСНОЕ МЕСТО СКРИПТА.
    #
    # Пароль генерируется РОВНО ОДИН РАЗ. Если сгенерировать его заново на
    # повторном прогоне, тома Postgres и ClickHouse останутся со старым, а
    # конфиг получит новый — контейнеры перестанут пускать к УЖЕ
    # СОБРАННЫМ данным. Выглядеть это будет как «база сломалась», а
    # чинится только ручным восстановлением старого пароля, которого
    # никто не записал.
    #
    # Поэтому: файл есть — не трогаем, что бы в нём ни лежало.
    if [ -f "$ENV_COMPOSE" ] && grep -q '^MANTA_DB_PASSWORD=' "$ENV_COMPOSE"; then
        ok "$ENV_COMPOSE уже содержит пароль — не перегенерирую"
    else
        [ "$CHECK_ONLY" = "1" ] && { warn "паролей ещё нет"; return; }
        local pw grafana
        pw=$(gen_password)
        grafana=$(gen_password)
        umask 077
        cat >"$ENV_COMPOSE" <<EOF
# Секреты docker compose для этой машины. Сгенерированы vps-bootstrap.sh.
# В git не попадают: deployments/.env в .gitignore.
#
# НЕ МЕНЯТЬ вручную после первого запуска: тома баз созданы с этим
# паролем, и правка конфига без правки томов закроет доступ к данным.
MANTA_DB_PASSWORD=$pw
GRAFANA_ADMIN_PASSWORD=$grafana
EOF
        chmod 600 "$ENV_COMPOSE"
        ok "сгенерирован $ENV_COMPOSE (0600)"
    fi

    # Пароль владельца БД и пароли прикладных сервисов — разные границы.
    # Первого достаточно, чтобы поднять Postgres и применить миграции;
    # остальные получают только групповые роли миграции 005. Добавляем
    # недостающие по одному: обновление старой VPS не меняет уже выданные
    # credentials и не обрывает живые соединения.
    local var
    for var in MANTA_DB_PASS_COLLECTOR MANTA_DB_PASS_REPORTS \
               MANTA_DB_PASS_GATEWAY MANTA_DB_PASS_RO; do
        if grep -q "^$var=" "$ENV_COMPOSE" 2>/dev/null; then
            continue
        fi
        if [ "$CHECK_ONLY" = "1" ]; then
            warn "$var ещё не создан"
        else
            printf '%s=%s\n' "$var" "$(gen_password)" >>"$ENV_COMPOSE"
        fi
    done
    [ "$CHECK_ONLY" = "1" ] || ok "сервисные пароли PostgreSQL на месте"

    # MinIO credentials разделяют входные данные и registry. Каждый
    # секрет добавляется независимо: обновление старой VPS не ротирует
    # уже выданные ключи и не требует останавливать весь конвейер.
    for var in MANTA_S3_PASS_INGEST MANTA_S3_PASS_PARSER \
               MANTA_S3_PASS_MODEL_READER MANTA_S3_PASS_MODEL_WRITER; do
        if grep -q "^$var=" "$ENV_COMPOSE" 2>/dev/null; then
            continue
        fi
        if [ "$CHECK_ONLY" = "1" ]; then
            warn "$var ещё не создан"
        else
            printf '%s=%s\n' "$var" "$(gen_password)" >>"$ENV_COMPOSE"
        fi
    done
    [ "$CHECK_ONLY" = "1" ] || ok "сервисные пароли MinIO на месте"

    # Отдельные ClickHouse identities и Redis AUTH. Добавление по одному
    # сохраняет уже выданные credentials при повторном bootstrap.
    for var in MANTA_CH_PASS_READER MANTA_CH_PASS_WRITER \
               MANTA_CH_PASS_TRAINER MANTA_REDIS_PASSWORD; do
        if grep -q "^$var=" "$ENV_COMPOSE" 2>/dev/null; then
            continue
        fi
        if [ "$CHECK_ONLY" = "1" ]; then
            warn "$var ещё не создан"
        else
            printf '%s=%s\n' "$var" "$(gen_password)" >>"$ENV_COMPOSE"
        fi
    done
    [ "$CHECK_ONLY" = "1" ] || ok "пароли ClickHouse и Redis на месте"

    # Compose должен знать host-side каталог bind mounts. Это не секрет,
    # но фиксируем один раз: смена пути без переноса файлов остановит gateway.
    if ! grep -q '^MANTA_KEYS_DIR=' "$ENV_COMPOSE" 2>/dev/null; then
        if [ "$CHECK_ONLY" = "1" ]; then
            warn "MANTA_KEYS_DIR ещё не задан"
        else
            printf 'MANTA_KEYS_DIR=%s\n' "${MANTA_KEYS_DIR:-$HOME/manta-keys}" >>"$ENV_COMPOSE"
        fi
    fi

    # Тот же пароль — скриптам на хосте. Они ходят в контейнеры напрямую
    # (docker exec clickhouse-client --password ...), и берут его из
    # ~/manta-train.env. Два файла, один пароль.
    local pw
    pw=$(grep '^MANTA_DB_PASSWORD=' "$ENV_COMPOSE" 2>/dev/null | cut -d= -f2-)
    [ -n "$pw" ] && [ "$CHECK_ONLY" != "1" ] || return 0

    touch "$ENV_HOST"; chmod 600 "$ENV_HOST"
    for var in CLICKHOUSE_PASSWORD POSTGRES_PASSWORD; do
        if grep -q "^$var=" "$ENV_HOST"; then
            # Уже задан — сверяем, а не переписываем: чужое значение может
            # быть осознанным, а молча его заменить значит сломать то, что
            # работало.
            grep -q "^$var=$pw$" "$ENV_HOST" || \
                warn "$var в $ENV_HOST не совпадает с deployments/.env"
        else
            printf '%s=%s\n' "$var" "$pw" >>"$ENV_HOST"
        fi
    done
    ok "$ENV_HOST согласован с deployments/.env"
}

# -- 2. docker -----------------------------------------------------------------

setup_docker() {
    say "docker"
    if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
        ok "уже установлен: $(docker --version | cut -d, -f1)"
        return
    fi
    [ "$CHECK_ONLY" = "1" ] && { warn "docker не установлен"; return; }
    need_root
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq ca-certificates curl gnupg
    curl -fsSL https://get.docker.com | $SUDO sh || die "установка docker не удалась"
    # Работать под root каждый раз не нужно, но и перелогин посреди
    # скрипта невозможен: группа применится со следующей сессии.
    [ "$(id -u)" = "0" ] || $SUDO usermod -aG docker "$USER" || true
    ok "docker установлен (перелогиньтесь, чтобы группа применилась)"
}

# -- 3. инструменты хоста ------------------------------------------------------
#
# Ровно то, чего требуют СОБСТВЕННОЕ расписание и СОБСТВЕННЫЕ инструкции
# этой установки, — не больше.
#
#   make    им написаны все команды в docs/SETUP-VPS.md и в напутствии в
#           конце этого скрипта. Ubuntu-сервер его не несёт, и первое же
#           «make peer-sync» после установки отвечает «command not found».
#   rclone  без него ночной обмен (04:00) и выгрузка бэкапа в облако
#           (03:30) падают КАЖДЫЙ раз. Оба скрипта проверяют наличие и
#           говорят внятно — но говорят они в cron, то есть в никуда.
#   nodejs  добыча солей у Game Coordinator (ежечасно) написана на Node и
#           иначе быть не может: питоновская библиотека Steam мертва —
#           `steam==1.4.4` шлёт ClientLogon 2019 года, и все CM отвечают
#           TryAnotherCM (спринты 165–167, проверено прямым сравнением с
#           тем же токеном). Без Node расписание планировало бы работу,
#           которую нечем выполнить.
#
# Общее правило, которого до спринта 157 не было: расписание для
# инструмента, которого нет на машине, — это не осторожность, а
# запланированный отказ. То же и с инструкцией: команда, напечатанная
# установщиком, обязана работать сразу после установки.
#
# Ставится из apt, а не «curl | sh»: версия из дистрибутива предсказуема и
# обновляется вместе с системой, а формат crypt-ремоутов rclone стабилен —
# конфиг с домашней машины читается и старым бинарём.
# Записаны как «команда:пакет», потому что это РАЗНЫЕ имена, и для node
# они не совпадают: apt ставит `nodejs`, а в PATH появляется `node`.
# Пока список был просто именами, проверка «уже стоит?» искала бы `nodejs`
# и не находила его на машине, где node есть, — то есть apt дёргался бы
# при каждом запуске установки. Двоеточия нет — команда и пакет
# называются одинаково.
HOST_TOOLS=(make:make rclone:rclone node:nodejs npm:npm)

setup_host_tools() {
    say "инструменты хоста"
    local entry cmd pkg missing="" names=""
    for entry in "${HOST_TOOLS[@]}"; do
        cmd="${entry%%:*}"
        pkg="${entry##*:}"
        names="$names $cmd"
        command -v "$cmd" >/dev/null || missing="$missing $pkg"
    done
    [ -n "$missing" ] || { ok "на месте:$names"; return; }
    [ "$CHECK_ONLY" = "1" ] && { warn "не установлены:$missing"; return; }
    need_root
    $SUDO apt-get update -qq
    # shellcheck disable=SC2086  — список слов, кавычки сделали бы из него один пакет
    $SUDO apt-get install -y -qq $missing || die "не удалось поставить:$missing"
    ok "установлены:$missing"
}

gateway_keys_dir() {
    local value=""
    value=$(grep '^MANTA_KEYS_DIR=' "$ENV_COMPOSE" 2>/dev/null | tail -1 | cut -d= -f2-)
    printf '%s' "${value:-${MANTA_KEYS_DIR:-$HOME/manta-keys}}"
}

setup_gateway_keys() {
    say "JWT и TLS gateway"
    local dir missing="" file
    dir=$(gateway_keys_dir)

    if [ "$CHECK_ONLY" != "1" ]; then
        ./scripts/gen-dev-keys.sh "$dir" >/dev/null || die "ключи gateway не созданы"
    fi
    for file in jwt-private.pem jwt-public.pem tls-cert.pem tls-key.pem; do
        [ -f "$dir/$file" ] || missing="$missing $file"
    done
    if [ -n "$missing" ]; then
        [ "$CHECK_ONLY" = "1" ] && { warn "нет файлов gateway:$missing"; return; }
        die "неполный комплект gateway:$missing"
    fi

    # Сертификат используется и host curl (localhost), и frontend/Prometheus
    # внутри Compose (api-gateway). Старый dev-сертификат без второго SAN
    # нельзя принять: клиенты правильно отвергнут его по имени.
    openssl x509 -in "$dir/tls-cert.pem" -noout -checkhost localhost >/dev/null 2>&1 &&
    openssl x509 -in "$dir/tls-cert.pem" -noout -checkhost api-gateway >/dev/null 2>&1 || {
        [ "$CHECK_ONLY" = "1" ] && { warn "TLS certificate не содержит SAN localhost и api-gateway"; return; }
        die "TLS certificate не содержит SAN localhost и api-gateway; перевыпустите комплект осознанно"
    }
    cmp <(openssl pkey -in "$dir/tls-key.pem" -pubout 2>/dev/null) \
        <(openssl x509 -in "$dir/tls-cert.pem" -pubkey -noout 2>/dev/null) >/dev/null ||
        die "TLS certificate и private key не образуют пару"
    cmp <(openssl pkey -in "$dir/jwt-private.pem" -pubout 2>/dev/null) \
        "$dir/jwt-public.pem" >/dev/null || die "JWT private/public keys не образуют пару"

    if [ "$CHECK_ONLY" != "1" ]; then
        # Distroless nonroot = uid/gid 65532. Ключ не world-readable, но
        # gateway получает read через группу bind-mounted файла.
        $SUDO chown root:65532 "$dir/tls-key.pem"
        $SUDO chmod 640 "$dir/tls-key.pem"
        chmod 600 "$dir/jwt-private.pem"
        chmod 644 "$dir/jwt-public.pem" "$dir/tls-cert.pem"

        touch "$ENV_HOST"; chmod 600 "$ENV_HOST"
        local name value
        for name in JWT_PRIVATE_KEY_FILE JWT_PUBLIC_KEY_FILE TLS_CERT_FILE TLS_KEY_FILE; do
            case "$name" in
                JWT_PRIVATE_KEY_FILE) value="$dir/jwt-private.pem";;
                JWT_PUBLIC_KEY_FILE)  value="$dir/jwt-public.pem";;
                TLS_CERT_FILE)        value="$dir/tls-cert.pem";;
                TLS_KEY_FILE)         value="$dir/tls-key.pem";;
            esac
            if grep -q "^$name=" "$ENV_HOST"; then
                grep -q "^$name=$value$" "$ENV_HOST" || warn "$name в $ENV_HOST указывает не на VPS-комплект"
            else
                printf '%s=%s\n' "$name" "$value" >>"$ENV_HOST"
            fi
        done
    fi
    local tls_acl jwt_acl
    tls_acl=$(stat -c '%a:%g' "$dir/tls-key.pem" 2>/dev/null)
    jwt_acl=$(stat -c '%a' "$dir/jwt-private.pem" 2>/dev/null)
    if [ "$tls_acl" != "640:65532" ] || [ "$jwt_acl" != "600" ]; then
        if [ "$CHECK_ONLY" = "1" ]; then
            warn "неверные права ключей: tls=$tls_acl (нужно 640:65532), jwt=$jwt_acl (нужно 600)"
            return
        fi
        die "не удалось выставить безопасные права ключей"
    fi
    ok "JWT verify key и TLS pair готовы ($dir)"
}

# -- 4. фаервол ----------------------------------------------------------------

setup_firewall() {
    say "фаервол"
    # Наложение docker-compose.vps.yml прибивает порты к 127.0.0.1, и это
    # основная защита. Фаервол — вторая линия: docker умеет пробивать
    # правила ufw, если однажды кто-то опубликует порт без loopback.
    if ! command -v ufw >/dev/null; then
        [ "$CHECK_ONLY" = "1" ] && { warn "ufw не установлен"; return; }
        need_root
        $SUDO apt-get install -y -qq ufw
    fi
    if $SUDO ufw status 2>/dev/null | grep -q '^Status: active'; then
        ok "ufw уже включён"
    else
        [ "$CHECK_ONLY" = "1" ] && { warn "ufw выключен"; return; }
        $SUDO ufw --force default deny incoming
        $SUDO ufw --force default allow outgoing
        # SSH разрешаем ДО включения: иначе скрипт отрежет сам себя от
        # машины, и чинить придётся через консоль хостера.
        $SUDO ufw allow OpenSSH
        $SUDO ufw --force enable
        ok "ufw включён, наружу только SSH"
    fi
}

# -- 5-6. стек и миграции ------------------------------------------------------

check_ports() {
    say "занятость портов"

    # ЗАЧЕМ ЗАРАНЕЕ. Без этой проверки конфликт вскрывается посреди
    # `compose up`: часть контейнеров уже создана, часть запущена, и в
    # конце — строка вида «failed to bind host port 127.0.0.1:5432/tcp:
    # address already in use». Из неё видно ОДИН порт из тринадцати, и
    # неизвестно, кто его занял; после исправления всё повторяется на
    # следующем порту.
    #
    # Первое живое развёртывание встало именно так: на машине уже работал
    # системный PostgreSQL.
    #
    # Список портов берётся из наложения для VPS, а не пишется руками:
    # записанный, он разошёлся бы при добавлении сервиса, и новый порт
    # проверялся бы «на живую», то есть никак.
    local ports
    ports=$(grep -oE '"127\.0\.0\.1:[0-9]+:' deployments/docker-compose.vps.yml |
        grep -oE '[0-9]+:$' | tr -d ':' | sort -un)
    if [ -z "$ports" ]; then
        warn "не удалось прочитать список портов из наложения — пропускаю"
        return
    fi

    # ss есть в Ubuntu из коробки; netstat — запасной путь.
    local lister=""
    command -v ss >/dev/null && lister="ss -tlnpH"
    [ -n "$lister" ] || { command -v netstat >/dev/null && lister="netstat -tlnp"; }
    if [ -z "$lister" ]; then
        warn "ни ss, ни netstat не найдены — проверить занятость нечем"
        return
    fi

    # ПОРТЫ, КОТОРЫЕ ДЕРЖИМ МЫ САМИ, конфликтом не считаются.
    #
    # Скрипт идемпотентен: его гоняют повторно после сбоя и после git
    # pull. На машине, где стек уже поднят, docker-proxy честно держит все
    # тринадцать портов — это и есть желаемое состояние, а не помеха.
    # Первая версия проверки об этом не знала и отказывалась работать на
    # успешно развёрнутой машине. Ложная тревога хуже отсутствия проверки:
    # она учит игнорировать себя.
    local ours=""
    if command -v docker >/dev/null && command -v python3 >/dev/null; then
        ours=$($COMPOSE --profile apps --profile monitoring ps --format json 2>/dev/null |
            python3 -c "import json,sys
out=set()
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try: rows=json.loads(line)
    except ValueError: continue
    for r in (rows if isinstance(rows,list) else [rows]):
        for pub in (r.get('Publishers') or []):
            if pub.get('PublishedPort'): out.add(str(pub['PublishedPort']))
print(' '.join(sorted(out)))" 2>/dev/null)
    fi

    local listening busy=""
    listening=$($lister 2>/dev/null)
    for port in $ports; do
        if [ -n "$ours" ] && printf '%s\n' $ours | grep -qx "$port"; then
            continue
        fi
        # Ищем ИМЕННО этот порт на loopback или на всех адресах: строка
        # «:15432» не должна считаться занятым 5432.
        if printf '%s\n' "$listening" |
           grep -qE "(127\.0\.0\.1|0\.0\.0\.0|\*|\[::\]):$port[[:space:]]"; then
            busy="$busy $port"
            local who
            who=$(printf '%s\n' "$listening" |
                grep -E "(127\.0\.0\.1|0\.0\.0\.0|\*|\[::\]):$port[[:space:]]" |
                grep -oE 'users:\(\("[^"]+"' | grep -oE '"[^"]+"' | tr -d '"' |
                head -1)
            warn "порт $port занят${who:+ процессом $who}"
        fi
    done

    if [ -n "$busy" ]; then
        echo
        echo "   Порты, нужные стеку, заняты ЧУЖИМИ процессами:$busy"
        echo "   (порты собственных контейнеров Manta сюда не попадают)"
        echo "   Обычная причина — системная служба, поставленная образом"
        echo "   VPS. Посмотреть и выключить, если она не нужна:"
        echo "       ss -tlnp | grep -E ':($(echo "$busy" | tr ' ' '|' | sed 's/^|//'))\\b'"
        echo "       systemctl disable --now postgresql   # к примеру"
        die "конфликт портов — стек не поднимался, ничего не сломано"
    fi
    if [ -n "$ours" ]; then
        ok "чужих процессов на наших портах нет ($(printf '%s\n' $ours | grep -c .) держит уже поднятый стек)"
    else
        ok "все $(printf '%s\n' "$ports" | grep -c .) портов свободны"
    fi
}

verify_running() {
    say "контейнеры живут, а не перезапускаются"

    # ЗАЧЕМ. `compose up -d` возвращает успех, как только контейнеры
    # СОЗДАНЫ. Упавший через секунду сервис под `restart: unless-stopped`
    # уходит в бесконечный цикл перезапусков — и установка при этом
    # рапортует «Готово».
    #
    # Так и вышло на живой машине: семь коллекторов подняты, один из них
    # перезапускается по кругу, а скрипт уже напечатал итог и вышел с
    # нулём. Про такую поломку узнают, когда заметят, что данные не
    # приходят.
    #
    # Ждём осознанно: сервисам нужно время на старт, и мгновенная
    # проверка объявила бы падением обычную инициализацию.
    local grace="${VERIFY_GRACE_S:-25}"
    printf '   даю %s с на старт...\n' "$grace"
    sleep "$grace"

    local bad=""
    for name in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep '^manta-'); do
        local state restarts
        state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)
        restarts=$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null)
        case "$state" in
            running)
                # Перезапуски бывают и у выживших: важно, что сейчас жив.
                [ "${restarts:-0}" -gt 3 ] && \
                    warn "$name живёт, но перезапускался $restarts раз"
                ;;
            restarting|exited|dead)
                bad="$bad $name"
                warn "$name: состояние $state, перезапусков ${restarts:-?}"
                ;;
        esac
    done

    if [ -n "$bad" ]; then
        echo
        echo "   Не работают:$bad"
        echo "   Последние строки каждого:"
        for name in $bad; do
            echo "   --- $name"
            docker logs --tail 12 "$name" 2>&1 | sed 's/^/       /'
        done
        die "часть сервисов не поднялась — стек НЕ готов к работе"
    fi
    ok "все контейнеры работают"
}

verify_gateway_security() {
    say "fail-closed gateway"
    local container="manta-api-gateway-1" dir env mounts published failed=""
    dir=$(gateway_keys_dir)
    if ! docker inspect "$container" >/dev/null 2>&1; then
        [ "$CHECK_ONLY" = "1" ] && { warn "$container не запущен — runtime-режим не проверен"; return; }
        die "$container не найден"
    fi

    env=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" 2>/dev/null)
    for expected in 'MANTA_PROD=1' \
                    'JWT_PUBLIC_KEY_FILE=/run/manta-secrets/jwt-public.pem' \
                    'TLS_CERT_FILE=/run/manta-secrets/tls-cert.pem' \
                    'TLS_KEY_FILE=/run/manta-secrets/tls-key.pem'; do
        printf '%s\n' "$env" | grep -qxF "$expected" || failed="$failed env:$expected"
    done
    mounts=$(docker inspect -f '{{range .Mounts}}{{println .Destination .RW}}{{end}}' "$container" 2>/dev/null)
    for target in jwt-public.pem tls-cert.pem tls-key.pem; do
        printf '%s\n' "$mounts" | grep -qxF "/run/manta-secrets/$target false" || \
            failed="$failed mount:$target"
    done
    published=$(docker port "$container" 8080/tcp 2>/dev/null)
    [ "$published" = "127.0.0.1:8080" ] || \
        failed="$failed publish:${published:-missing}"
    if [ -n "$failed" ]; then
        [ "$CHECK_ONLY" = "1" ] && { warn "gateway не fail-closed:$failed"; return; }
        die "gateway не fail-closed:$failed"
    fi

    if curl -fsS --cacert "$dir/tls-cert.pem" https://localhost:8080/healthz >/dev/null 2>&1; then
        ok "gateway отвечает по проверенному TLS; JWT verify key и mounts активны"
    elif [ "$CHECK_ONLY" = "1" ]; then
        warn "gateway не отвечает по проверенному HTTPS на localhost:8080"
    else
        die "gateway не отвечает по проверенному HTTPS на localhost:8080"
    fi
}

build_images() {
    say "сборка образов (по одному)"

    # ПО ОДНОМУ, а не разом, и это не про аккуратность.
    #
    # `compose up --build` собирает ВСЕ образы параллельно. На домашней
    # машине это удобно, на двухъядерном VPS — нет: одновременно идут три
    # pip install, npm ci, две сборки Go и компиляция C++-ядра. Памяти не
    # хватает, и падает то, чему не повезло.
    #
    # Хуже другое. Когда одна сборка падает, остальные семь получают
    # CANCELED, и в выводе остаются двести строк отменённых шагов, среди
    # которых настоящей ошибки уже не найти. Первый живой прогон на VPS
    # именно так и закончился: `go mod download` вернул код 1, а ЧТО он
    # сказал — не сохранилось.
    #
    # Последовательная сборка медленнее, но отвечает на вопрос «что
    # сломалось» с первого раза.
    local log="/tmp/manta-build.log"
    : >"$log"

    # Список собираемых сервисов берётся из САМОГО compose, а не пишется
    # руками: записанный список разошёлся бы при добавлении сервиса, и
    # новый молча не собирался бы. json + stdlib-модуль json — PyYAML на
    # голой Ubuntu нет, а python3 есть.
    local buildable=""
    if command -v python3 >/dev/null; then
        buildable=$($COMPOSE --profile apps config --format json 2>/dev/null |
            python3 -c "import json,sys
d=json.load(sys.stdin)
print(' '.join(n for n,s in d.get('services',{}).items() if 'build' in s))" 2>/dev/null)
    fi

    if [ -z "$buildable" ]; then
        # Не смогли разобрать — собираем всё одной командой, но с
        # выключенным bake: без него compose строит последовательно.
        warn "список сервисов не разобран, собираю всё разом"
        COMPOSE_BAKE=false $COMPOSE --profile apps build 2>&1 | tee -a "$log" | tail -40 \
            || die "сборка не удалась, полный вывод: $log"
        ok "образы собраны"
        return
    fi

    local failed=""
    for svc in $buildable; do
        printf '   собираю %s ...\n' "$svc"
        if COMPOSE_BAKE=false $COMPOSE --profile apps build "$svc" >>"$log" 2>&1; then
            printf '   OK   %s\n' "$svc"
        else
            failed="$failed $svc"
            warn "$svc не собрался, последние строки:"
            tail -25 "$log" | sed 's/^/      /'
        fi
    done
    [ -z "$failed" ] || die "не собрались:$failed — полный вывод в $log"
    ok "образы собраны"
}

load_host_settings() {
    # Настройки машины (шард, ключи API) живут в ~/manta-train.env, а
    # compose подставляет переменные из СВОЕГО окружения. Дома этой
    # проблемы нет: там сервисы читают файл сами, потому что запускаются
    # процессами. В Docker его не видит никто.
    local f="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
    [ -f "$f" ] || return 0
    set -a
    # shellcheck disable=SC1090
    . "$f"
    set +a
}

collector_profiles() {
    # Условные коллекторы включаются ровно по тем же признакам, что и в
    # dev-recover.sh, — иначе две машины собирали бы разное при
    # одинаковом файле настроек.
    #
    # Почему профилями, а не «пусть падает»: STRATZ без токена падает на
    # старте, а restart: unless-stopped превратил бы это в ровный шум в
    # логах — поломка, неотличимая от работы.
    local p=""
    [ -n "${STRATZ_API_TOKEN:-}" ] && p="$p --profile stratz"
    [ "${CANDIDATES_ENABLED:-0}" = "1" ] && p="$p --profile candidates"
    printf '%s' "$p"
}

start_stack() {
    say "стек"
    if [ "$CHECK_ONLY" = "1" ]; then
        # Каждая ветка ГОВОРИТ. Раньше при отсутствии docker раздел
        # молчал совсем: `compose ps` писал в /dev/null, и на чистой
        # машине «стек» выглядел единственным пунктом без замечаний —
        # то есть исправным. Молчание не должно быть неотличимо от «всё
        # хорошо»; в этом проекте такая тишина уже стоила тринадцати
        # дней без бэкапов.
        if ! command -v docker >/dev/null; then
            warn "проверить нечем: docker не установлен"
            return
        fi
        local running
        running=$($COMPOSE ps --format '{{.Name}}' 2>/dev/null | grep -c . || true)
        if [ "$running" = "0" ]; then
            warn "стек не поднят (контейнеров нет)"
        else
            ok "контейнеров запущено: $running"
        fi
        verify_gateway_security
        return
    fi
    load_host_settings
    check_ports
    build_images
    local profiles
    profiles=$(collector_profiles)
    if [ -n "$profiles" ]; then
        ok "дополнительные коллекторы:$profiles"
    else
        warn "stratz и candidates выключены (нет токена / не включены)"
    fi
    # Сначала только инфраструктура. Прикладные контейнеры уже настроены
    # на manta_*_user, но login-роли появляются лишь после миграции 005 и
    # create-db-users.sh. Поднять всё одним вызовом — получить restart-loop
    # каждого клиента Postgres в середине штатной установки.
    $COMPOSE up -d || die "инфраструктура compose не поднялась"
    ok "инфраструктура поднята"

    say "ожидание готовности баз"
    for _ in $(seq 1 60); do
        if docker exec manta-clickhouse-1 clickhouse-client \
             --user dota --password "$(grep '^MANTA_DB_PASSWORD=' "$ENV_COMPOSE" | cut -d= -f2-)" \
             -q "SELECT 1" >/dev/null 2>&1; then
            ok "ClickHouse отвечает"
            break
        fi
        sleep 5
    done

    say "миграции"
    ./scripts/pg-migrate.sh && ./scripts/ch-migrate.sh || die "миграции не прошли"
    ok "миграции применены"

    say "пользователи PostgreSQL"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_COMPOSE"
    set +a
    POSTGRES_CONTAINER=manta-postgres-1 \
    PGPASSWORD="$MANTA_DB_PASSWORD" \
        ./scripts/create-db-users.sh || die "сервисные пользователи PostgreSQL не созданы"
    ok "сервисные пользователи и роли применены"

    say "пользователи ClickHouse"
    CLICKHOUSE_CONTAINER=manta-clickhouse-1 \
    CLICKHOUSE_ADMIN_PASSWORD="$MANTA_DB_PASSWORD" \
        ./scripts/create-clickhouse-users.sh || \
        die "сервисные пользователи ClickHouse не созданы"
    ok "функциональные пользователи и роли ClickHouse применены"

    say "пользователи MinIO"
    for _ in $(seq 1 60); do
        if docker exec manta-minio-1 mc ready local >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    MINIO_CONTAINER=manta-minio-1 \
    MINIO_ROOT_PASSWORD="$MANTA_DB_PASSWORD" \
        ./scripts/create-minio-users.sh || die "сервисные пользователи MinIO не созданы"
    ok "бакеты, пользователи и политики MinIO применены"

    # Теперь login-роли и object-storage policies существуют, можно
    # запускать их клиентов.
    # shellcheck disable=SC2086
    $COMPOSE --profile apps $profiles up -d || die "приложения compose не поднялись"
    ok "приложения подняты"

    verify_running
    verify_gateway_security
}

# -- 6. расписание -------------------------------------------------------------

CRON_MARKER="# manta-vps"

# Каким расписание ДОЛЖНО быть на этой машине.
#
# Порядок в сутках: сначала свой бэкап, потом обмен с соседями, потом
# сторож — он и доложит, что предыдущие две части прошли.
#
# Обмен ставится ТОЛЬКО при заданных соседях. Одиночная машина иначе
# получала бы «ОБМЕН НЕ УДАЛСЯ» в Telegram каждую ночь: расписание
# планировало бы работу, которой на этой машине не существует.
cron_wanted() {
    printf '%s\n' "$CRON_MARKER"
    printf '30 3 * * * cd %s && ./scripts/backup.sh\n' "$REPO"
    [ -n "${MANTA_PEER_HOSTS:-}" ] && \
        printf '0 4 * * * cd %s && ./scripts/peer-sync.sh\n' "$REPO"
    printf '0 9 * * * cd %s && ./scripts/heartbeat.sh\n' "$REPO"
    # Соли GC — каждый час маленькими порциями (спринт 173). Бюджет
    # аккаунта НАКОПИТЕЛЬНЫЙ: он копится в простое и отдаётся по капле,
    # поэтому частые короткие заходы берут больше, чем один длинный
    # прогон, а замеры прямо показали, что «спрашивать медленнее» не
    # помогает — помогает спрашивать меньше за раз.
    #
    # Ставится БЕЗУСЛОВНО, в отличие от обмена. Разница в том, что обмен
    # без соседей — работа, которой не существует, а добыча без токена
    # просто выключена: `gc-salts.sh` сам завершается спокойно и молча.
    # Дублировать эту проверку здесь значило бы завести два места,
    # решающих одно и то же, — а они однажды решат по-разному.
    #
    # Минута 17, а не 0: в начале часа уже стоят чужие задачи, и делить с
    # ними одну минуту незачем.
    printf '17 * * * * cd %s && ./scripts/gc-salts.sh\n' "$REPO"
    return 0
}

# Наши строки в текущем расписании и все остальные. Разделяются по
# маркеру и по пути репозитория: чужие задачи владельца скрипт переписывает
# целиком, и вернуть их на место обязан до единой.
cron_mine() {
    crontab -l 2>/dev/null |
        grep -F -e "$CRON_MARKER" -e "cd $REPO && ./scripts/" || true
}

cron_foreign() {
    crontab -l 2>/dev/null |
        grep -vF -e "$CRON_MARKER" -e "cd $REPO && ./scripts/" || true
}

setup_cron() {
    say "расписание"
    # Настройки читаются здесь заново: их загружает start_stack, а от него
    # нас отделяет несколько функций — полагаться на то, что переменная
    # «ещё жива», значит однажды тихо её потерять.
    load_host_settings

    # Всё читается ДО первой записи. `{ cron_foreign; cron_wanted; } |
    # crontab -` выглядит короче, но обе стороны конвейера стартуют
    # одновременно: `crontab -` открывает расписание на запись и обрезает
    # его раньше, чем `cron_foreign` успевает его прочитать. Чужие задачи
    # владельца при этом исчезают молча — заметить пропажу можно и через
    # месяц. Нашла это проверка test_foreign_cron_lines_survive.
    local want have foreign
    want=$(cron_wanted)
    have=$(cron_mine)
    foreign=$(cron_foreign)

    if [ "$want" = "$have" ]; then
        ok "cron уже настроен"
        return
    fi

    # Сверяется СОДЕРЖИМОЕ, а не наличие маркера. Прежняя версия выходила
    # по первому совпадению с маркером — и расписание, заведённое старой
    # версией скрипта, оставалось прежним навсегда: правки доезжали до
    # чистых машин и не доезжали до работающих. Ту же беду в спринтах
    # 159–161 пришлось чинить тремя отдельными коммитами уже на живой VPS.
    if [ "$CHECK_ONLY" = "1" ]; then
        if [ -z "$have" ]; then warn "cron не настроен"
        else warn "cron отличается от нужного — прогон без --check починит"; fi
        return
    fi
    { [ -n "$foreign" ] && printf '%s\n' "$foreign"; cron_wanted; } | crontab -
    # Перечисляется РОВНО то, что поставлено. Баннер, называющий не весь
    # список, читается как полный: шаг, о котором он молчит, считается
    # неслучившимся, и его идут заводить руками (или, хуже, не идут).
    if [ -n "${MANTA_PEER_HOSTS:-}" ]; then
        ok "бэкап 03:30, обмен 04:00, сторож 09:00, соли GC ежечасно (UTC)"
    else
        ok "бэкап 03:30, сторож 09:00, соли GC ежечасно (UTC); обмена нет — соседи не заданы"
    fi
}

# -- поехали -------------------------------------------------------------------

echo "=================================================================="
echo "  Manta на VPS: $( [ "$CHECK_ONLY" = 1 ] && echo проверка || echo установка )"
echo "  каталог: $REPO"
echo "=================================================================="

for step in "${STEPS[@]}"; do
    wanted_step "${step%%:*}" && "${step##*:}"
done

echo
echo "=================================================================="
if [ -n "$ONLY" ]; then
    echo "  Выполнены только шаги: $ONLY"
elif [ "$CHECK_ONLY" = "1" ]; then
    echo "  Проверка закончена."
else
    # Список подсказок собирается ПО СОСТОЯНИЮ машины, а не печатается
    # одинаковым всегда. Предлагать «залить датасет: make peer-sync» там,
    # где соседей нет, — значит послать за тем, чего не существует; ровно
    # так эта установка уже предлагала команду `make`, которой на машине
    # не было (спринт 157).
    echo "  Готово. Дальше — docs/SETUP-VPS.md:"
    [ -f "$HOME/.config/rclone/rclone.conf" ] || \
        echo "   • rclone не настроен: бэкап в облако выключен (OAuth нужен браузер)"
    grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$ENV_HOST" 2>/dev/null || \
        echo "   • Telegram не настроен — на VPS это единственная телеметрия"
    echo "   • ключи API и темп сбора — в $ENV_HOST"
    [ -n "${MANTA_PEER_HOSTS:-}" ] && \
        echo "   • забрать слепки соседей: make peer-sync"
    echo "   • Grafana — только через SSH-туннель, наружу порты закрыты"
fi
echo "=================================================================="
