#!/usr/bin/env bash
# Ключи и сертификаты для локального стенда (спринт 58).
#
#   ./scripts/gen-dev-keys.sh [каталог]     # по умолчанию ~/manta-keys
#
# Создаёт:
#   jwt-private.pem / jwt-public.pem — пара RS256 для подписи токенов;
#   tls-cert.pem / tls-key.pem       — самоподписанный сертификат TLS 1.3
#                                      на localhost (SAN: localhost, 127.0.0.1).
#
# ВАЖНО: это ключи РАЗРАБОТКИ. Самоподписанный сертификат браузер и curl
# принимают только с явным доверием (curl --cacert / -k). Для прода —
# сертификат от CA и ключи из секрет-менеджера (docs/security-review.md §5).
#
# Ключи кладутся ВНЕ репозитория и с правами 600: в git им не место.
set -euo pipefail

DIR="${1:-$HOME/manta-keys}"
mkdir -p "$DIR"
chmod 700 "$DIR"

if [ -f "$DIR/jwt-private.pem" ]; then
    echo ">> $DIR/jwt-private.pem уже существует — пропуск"
    echo "   (удалите файлы вручную, чтобы перевыпустить; это инвалидирует"
    echo "    все выданные токены)"
else
    echo ">> RSA-пара для RS256"
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
        -out "$DIR/jwt-private.pem" 2>/dev/null
    openssl rsa -in "$DIR/jwt-private.pem" -pubout \
        -out "$DIR/jwt-public.pem" 2>/dev/null
    chmod 600 "$DIR/jwt-private.pem"
    chmod 644 "$DIR/jwt-public.pem"
fi

if [ -f "$DIR/tls-cert.pem" ]; then
    echo ">> $DIR/tls-cert.pem уже существует — пропуск"
else
    echo ">> самоподписанный сертификат TLS (localhost, 365 дней)"
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
        -keyout "$DIR/tls-key.pem" -out "$DIR/tls-cert.pem" \
        -subj "/CN=localhost/O=Manta dev" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
    chmod 600 "$DIR/tls-key.pem"
    chmod 644 "$DIR/tls-cert.pem"
fi

# Соль псевдонимизации никнеймов (Гл. 9.7, спринт 70). Секрет: без неё
# псевдоним подбирается перебором по публичному списку ников, то есть
# псевдонимизацией быть перестаёт. Меняя соль, вы теряете связь со всеми
# уже записанными псевдонимами — GDPR-поиск по старым строкам перестанет
# находить субъекта. Поэтому файл создаётся ОДИН раз и не перегенерируется.
if [ -f "$DIR/pii-salt" ]; then
    echo ">> $DIR/pii-salt уже существует — пропуск (перегенерация разорвала бы связь со старыми псевдонимами)"
else
    echo ">> соль псевдонимизации никнеймов"
    openssl rand -hex 32 > "$DIR/pii-salt"
    chmod 600 "$DIR/pii-salt"
fi

cat <<EOF

Готово: $DIR

Включить в env-файле (MANTA_TRAIN_ENV, обычно ~/manta-train.env):

  JWT_PRIVATE_KEY_FILE=$DIR/jwt-private.pem
  JWT_PUBLIC_KEY_FILE=$DIR/jwt-public.pem
  TLS_CERT_FILE=$DIR/tls-cert.pem
  TLS_KEY_FILE=$DIR/tls-key.pem

Псевдонимизация никнеймов (Гл. 9.7) — по умолчанию ВЫКЛЮЧЕНА, стенд
работает как раньше. Для прода включить:

  MANTA_PII_MODE=pseudonymize
  MANTA_PII_SALT=\$(cat $DIR/pii-salt)

Соль не менять после начала сбора: она связывает ник с уже записанными
псевдонимами, и смена сделает старые строки ненаходимыми по GDPR.

После этого перезапустить gateway (make recover). Проверка:

  curl -k https://localhost:8080/healthz
  curl -sk https://localhost:8080/.well-known/jwks.json

Токен администратора (нужен для выпуска остальных) выпускается только
инстансом с приватным ключом; первый токен — напрямую скриптом:

  ./scripts/issue-token.sh admin
EOF
