#!/usr/bin/env bash
# Развёртывание Manta на чистом VPS (спринт 143).
#
#     ./scripts/vps-bootstrap.sh            # полная установка
#     ./scripts/vps-bootstrap.sh --check    # только проверить готовность
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
#   3. Фаервол: наружу только SSH. Всё остальное — через SSH-туннель.
#   4. Стек с наложением docker-compose.vps.yml (порты на 127.0.0.1,
#      потолки памяти).
#   5. Миграции.
#   6. Расписание: бэкап, обмен с домашней машиной, сторож.
#
# ЧЕГО ОН НЕ ДЕЛАЕТ. Не трогает уже сгенерированный пароль — см. ниже, это
# самое опасное место всего скрипта. Не настраивает rclone (нужен браузер
# для OAuth — делается с домашней машины, конфиг переносится). Не заливает
# датасет: это отдельный шаг, см. docs/SETUP-VPS.md.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)

ENV_COMPOSE="$REPO/deployments/.env"
ENV_HOST="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
COMPOSE="docker compose -f deployments/docker-compose.yml -f deployments/docker-compose.vps.yml"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\n>> %s\n' "$1"; }
ok()   { printf '   OK   %s\n' "$1"; }
warn() { printf '   ВНИМАНИЕ %s\n' "$1"; }
die()  { printf '\nОСТАНОВ: %s\n' "$1" >&2; exit 1; }

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

# -- 3. фаервол ----------------------------------------------------------------

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

# -- 4-5. стек и миграции ------------------------------------------------------

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
    # shellcheck disable=SC2086
    $COMPOSE --profile apps $profiles up -d || die "compose up не удался"
    ok "контейнеры подняты"

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

    verify_running
}

# -- 6. расписание -------------------------------------------------------------

setup_cron() {
    say "расписание"
    local marker="# manta-vps"
    if crontab -l 2>/dev/null | grep -qF "$marker"; then
        ok "cron уже настроен"
        return
    fi
    [ "$CHECK_ONLY" = "1" ] && { warn "cron не настроен"; return; }
    # Порядок в сутках: сначала свой бэкап, потом обмен с домашней
    # машиной, потом сторож — он и доложит, что обе части прошли.
    { crontab -l 2>/dev/null;
      printf '%s\n' "$marker"
      printf '30 3 * * * cd %s && ./scripts/backup.sh\n' "$REPO"
      printf '0 4 * * * cd %s && ./scripts/peer-sync.sh\n' "$REPO"
      printf '0 9 * * * cd %s && ./scripts/heartbeat.sh\n' "$REPO"
    } | crontab -
    ok "бэкап 03:30, обмен 04:00, сторож 09:00 (UTC)"
}

# -- поехали -------------------------------------------------------------------

echo "=================================================================="
echo "  Manta на VPS: $( [ "$CHECK_ONLY" = 1 ] && echo проверка || echo установка )"
echo "  каталог: $REPO"
echo "=================================================================="

setup_secrets
setup_docker
setup_firewall
start_stack
setup_cron

echo
echo "=================================================================="
if [ "$CHECK_ONLY" = "1" ]; then
    echo "  Проверка закончена."
else
    echo "  Готово. Дальше — docs/SETUP-VPS.md:"
    echo "   • перенести rclone.conf с домашней машины (OAuth нужен браузер)"
    echo "   • дописать в $ENV_HOST метку, шард и ключи API"
    echo "   • залить датасет: make peer-sync"
    echo "   • Grafana — только через SSH-туннель, наружу порты закрыты"
fi
echo "=================================================================="
