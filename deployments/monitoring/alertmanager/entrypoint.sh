#!/bin/sh
# Генерация конфига Alertmanager из окружения (Гл. 11.6). Telegram-роут
# включается, только если заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID —
# те же секреты, что у ml-autotrain. Без них алерты видны в UI
# Alertmanager (localhost:9093), но никуда не отправляются.
# Alertmanager не умеет подставлять env в конфиг — поэтому генерируем.
set -eu
CFG=/tmp/alertmanager.yml

cat > "$CFG" <<EOF
route:
  receiver: default
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    # critical напоминает чаще (в production здесь будет PagerDuty).
    - matchers: ['severity="critical"']
      repeat_interval: 1h

receivers:
  - name: default
EOF

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  printf '%s' "$TELEGRAM_BOT_TOKEN" > /tmp/tg_token
  cat >> "$CFG" <<EOF
    telegram_configs:
      - bot_token_file: /tmp/tg_token
        chat_id: ${TELEGRAM_CHAT_ID}
        parse_mode: ''
        send_resolved: true
EOF
fi

exec /bin/alertmanager --config.file="$CFG" --storage.path=/alertmanager "$@"
