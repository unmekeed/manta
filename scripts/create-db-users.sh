#!/usr/bin/env bash
# Пользователи БД для входа (спринт 73). Дополняет миграцию 005, которая
# создаёт только групповые роли с правами.
#
# Пароли берутся из окружения и НИКОГДА не попадают в репозиторий — это и
# есть причина, по которой пользователи создаются скриптом, а не миграцией:
# миграции лежат в git, секреты — нет.
#
#   MANTA_DB_PASS_COLLECTOR=... MANTA_DB_PASS_REPORTS=... \
#   MANTA_DB_PASS_GATEWAY=...   MANTA_DB_PASS_RO=... \
#     ./scripts/create-db-users.sh
#
# Идемпотентен: существующему пользователю пароль обновляется, роль
# переприсваивается. Ничего не отбирает у `dota` — переключение сервисов на
# новых пользователей делается заменой POSTGRES_DSN в env-файле, и откат
# такой же простой.
set -euo pipefail
cd "$(dirname "$0")/.."

PGHOST="${POSTGRES_HOST:-localhost}"
PGUSER="${POSTGRES_USER:-dota}"
PGDB="${POSTGRES_DB:-manta}"
export PGPASSWORD="${PGPASSWORD:-dota_dev_password}"
PG_CONTAINER="${POSTGRES_CONTAINER:-manta-postgres-1}"

# На чистой VPS psql на хост не ставится: клиент уже есть в образе
# postgres. Дома и в ручном запуске сохраняем прежний fallback на host
# psql, если контейнера нет.
admin_psql() {
    if command -v docker >/dev/null 2>&1 &&
       docker inspect "$PG_CONTAINER" >/dev/null 2>&1; then
        docker exec -i -e PGPASSWORD="$PGPASSWORD" "$PG_CONTAINER" \
            psql -U "$PGUSER" -d "$PGDB" "$@"
    else
        psql -h "$PGHOST" -U "$PGUSER" -d "$PGDB" "$@"
    fi
}

created=0
make_user() {
    local user="$1" role="$2" pass="$3"
    if [ -z "$pass" ]; then
        echo "   пропуск $user — пароль не задан в окружении"
        return
    fi
    # Имя и пароль подставляются psql-переменными через format() с %I/%L,
    # а не конкатенацией строк: спецсимволы в пароле иначе ломают запрос.
    #
    # Почему \gexec, а не DO $$ … $$: psql НЕ подставляет свои переменные
    # внутрь долларовых кавычек — там :'user' остаётся текстом и даёт
    # синтаксическую ошибку. Поэтому запрос собирается на верхнем уровне
    # и исполняется \gexec.
    #
    # ON_ERROR_STOP=1 обязателен: без него psql возвращает 0 даже после
    # ошибки, и скрипт рапортует об успехе, ничего не создав.
    admin_psql -qtA -v ON_ERROR_STOP=1 \
        -v user="$user" -v pass="$pass" -v role="$role" <<'SQL' >/dev/null
SELECT format('%s ROLE %I LOGIN PASSWORD %L',
              CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user')
                   THEN 'ALTER' ELSE 'CREATE' END,
              :'user', :'pass')
\gexec
SELECT format('GRANT %I TO %I', :'role', :'user')
\gexec
SQL
    echo "   $user -> роль $role"
    created=$((created + 1))
}

echo ">> пользователи БД (пароли — из окружения, не из репозитория)"
make_user manta_collector_user manta_collector "${MANTA_DB_PASS_COLLECTOR:-}"
make_user manta_reports_user   manta_reports   "${MANTA_DB_PASS_REPORTS:-}"
make_user manta_gateway_user   manta_gateway   "${MANTA_DB_PASS_GATEWAY:-}"
make_user manta_ro_user        manta_ro        "${MANTA_DB_PASS_RO:-}"

if [ "$created" -eq 0 ]; then
    echo
    echo "Ничего не создано: задайте пароли, например"
    echo "  MANTA_DB_PASS_COLLECTOR=\$(openssl rand -hex 16) ..."
    exit 1
fi

cat <<'HINT'

Готово. Переключить сервисы — заменить POSTGRES_DSN в env-файле:

  # data-collector
  POSTGRES_DSN=postgresql://manta_collector_user:ПАРОЛЬ@localhost:5432/manta
  # report-generator
  POSTGRES_DSN=postgresql://manta_reports_user:ПАРОЛЬ@localhost:5432/manta
  # api-gateway
  POSTGRES_DSN=postgresql://manta_gateway_user:ПАРОЛЬ@localhost:5432/manta

Пользователь `dota` не тронут — откат делается возвратом прежнего DSN.
HINT
