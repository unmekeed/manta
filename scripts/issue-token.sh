#!/usr/bin/env bash
# Выпуск access-токена локально, приватным ключом (спринт 58).
#
#   ./scripts/issue-token.sh <роль> [subject] [plan]
#   ./scripts/issue-token.sh admin
#
# Нужен, чтобы получить ПЕРВЫЙ admin-токен: эндпоинт POST /auth/token сам
# требует роль admin, и без этого скрипта система была бы замкнута на себя.
# Дальше токены удобнее выпускать через API.
#
# Ключ берётся из JWT_PRIVATE_KEY_FILE (env-файл MANTA_TRAIN_ENV или
# окружение). Токен печатается в stdout — не сохраняйте его в файлы репо.
set -euo pipefail

TRAIN_ENV="${MANTA_TRAIN_ENV:-$HOME/manta-train.env}"
[ -f "$TRAIN_ENV" ] && { set -a; . "$TRAIN_ENV"; set +a; }

ROLE="${1:?роль: anonymous|free|premium|pro|admin|service}"
SUBJECT="${2:-local-$ROLE}"
PLAN="${3:-}"
KEY="${JWT_PRIVATE_KEY_FILE:?не задан JWT_PRIVATE_KEY_FILE (см. scripts/gen-dev-keys.sh)}"
TTL="${JWT_ACCESS_TTL_SECONDS:-900}"

b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

now=$(date +%s)
header='{"alg":"RS256","typ":"JWT"}'
payload=$(printf '{"sub":"%s","role":"%s","plan":"%s","iat":%d,"exp":%d,"jti":"%s"}' \
    "$SUBJECT" "$ROLE" "$PLAN" "$now" "$((now + TTL))" "$(openssl rand -hex 16)")

signing_input="$(printf '%s' "$header" | b64).$(printf '%s' "$payload" | b64)"
signature=$(printf '%s' "$signing_input" |
    openssl dgst -sha256 -sign "$KEY" -binary | b64)

echo "$signing_input.$signature"
