#!/usr/bin/env bash
# Security scan (D6 роадмапа, Гл. 9 спеки): секреты, уязвимости зависимостей,
# дефолтные креды, права на файлы.
#
#   make security-scan            # полный прогон
#   SCAN_HISTORY=0 make security-scan   # без обхода git-истории (быстрее)
#
# Инструменты (pip-audit, govulncheck, npm audit) подключаются, если
# установлены; отсутствие инструмента — WARN, а не тихий пропуск: «сканер
# не запускался» и «уязвимостей нет» должны выглядеть по-разному.
#
# Выход: 0 — критичных находок нет; 1 — есть (CI валит сборку).
set -uo pipefail
cd "$(dirname "$0")/.."

crit=0; warn=0
ok()   { printf '   \033[32m OK \033[0m %s\n' "$*"; }
wrn()  { printf '   \033[33mWARN\033[0m %s\n' "$*"; warn=$((warn + 1)); }
bad()  { printf '   \033[31mCRIT\033[0m %s\n' "$*"; crit=$((crit + 1)); }

echo "== Секреты в репозитории (Гл. 9.6)"
# Реальные секреты: токен Telegram-бота (123456:AA...), ключи OpenDota/
# Anthropic, приватные ключи. Дефолтный dev-пароль ищем отдельно ниже.
PATTERNS='[0-9]{8,10}:AA[0-9A-Za-z_-]{30,}|sk-ant-[0-9A-Za-z_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key\s*=|OPENDOTA_API_KEY\s*=\s*[0-9a-f]{8}'
hits=$(git ls-files -z | xargs -0 grep -nEI "$PATTERNS" 2>/dev/null | grep -v '^scripts/security-scan.sh:' || true)
if [ -n "$hits" ]; then
    bad "секреты в рабочем дереве:"; echo "$hits" | sed 's/^/        /'
else
    ok "в отслеживаемых файлах секретов не найдено"
fi

if [ "${SCAN_HISTORY:-1}" = "1" ]; then
    hist=$(git log -p --all -S'AA' --pickaxe-regex \
             -G'[0-9]{8,10}:AA[0-9A-Za-z_-]{30,}' --oneline 2>/dev/null | head -5 || true)
    if [ -n "$hist" ]; then
        bad "похоже на токен бота в истории коммитов:"; echo "$hist" | sed 's/^/        /'
    else
        ok "в истории коммитов токенов не найдено"
    fi
fi

# env-файлы не должны быть под контролем версий ни при каких условиях.
tracked_env=$(git ls-files | grep -E '(^|/)\.env$|\.env\.(local|prod)' || true)
if [ -n "$tracked_env" ]; then
    bad "env-файлы под git: $tracked_env"
else
    ok ".env-файлы не отслеживаются (только .env.example — шаблон)"
fi

echo "== Права на файлы секретов"
for f in "${MANTA_TRAIN_ENV:-$HOME/manta-train.env}" deployments/.env; do
    [ -f "$f" ] || continue
    perm=$(stat -c %a "$f")
    case "$perm" in
        600|400) ok "$f — права $perm" ;;
        *) wrn "$f — права $perm (рекомендуется 600: chmod 600 $f)" ;;
    esac
done

echo "== Дефолтные креды (Гл. 9.6: прод обязан их переопределить)"
defaults=$(git ls-files | xargs grep -lE 'dota_dev_password' 2>/dev/null | wc -l)
if [ -n "${MANTA_PROD:-}" ]; then
    bad "MANTA_PROD задан, но в коде $defaults файлов с dev-паролем по умолчанию"
else
    wrn "dev-креды (dota/dota_dev_password) как fallback в $defaults файлах — допустимо для локального стенда, для прод-развёртывания задать POSTGRES_PASSWORD/CLICKHOUSE_PASSWORD/S3_* через секрет-менеджер"
fi

echo "== Уязвимости зависимостей (SCA)"
if command -v pip-audit >/dev/null; then
    for req in apps/*/requirements.txt; do
        out=$(pip-audit -r "$req" --progress-spinner off 2>&1) || true
        if echo "$out" | grep -qi "no known vulnerabilities"; then
            ok "pip-audit $(basename "$(dirname "$req")")"
        else
            wrn "pip-audit $(basename "$(dirname "$req")"): $(echo "$out" | tail -3 | tr '\n' ' ')"
        fi
    done
else
    wrn "pip-audit не установлен (pip install pip-audit) — Python-зависимости НЕ проверены"
fi

if command -v govulncheck >/dev/null; then
    # С спринта 68 базовая линия — НОЛЬ достижимых уязвимостей в обоих
    # модулях (тулчейн поднят до актуального security patch release).
    # Поэтому находка теперь CRIT, а не WARN: это регресс, а не известный
    # долг. govulncheck даёт ненулевой код только когда уязвимость
    # ДОСТИЖИМА из нашего кода — транзитивные, которые мы не вызываем,
    # сборку не валят.
    for svc in apps/api-gateway apps/replay-parser/svc; do
        output=""
        if output=$(cd "$svc" && govulncheck ./... 2>&1); then
            ok "govulncheck $svc"
        else
            printf '%s\n' "$output" >&2
            bad "govulncheck $svc — достижимые уязвимости (базовая линия 0): govulncheck ./... в $svc"
        fi
    done
else
    wrn "govulncheck не установлен (go install golang.org/x/vuln/cmd/govulncheck@latest) — Go-зависимости НЕ проверены"
fi

if command -v npm >/dev/null && [ -f apps/frontend/package.json ]; then
    audit=$(cd apps/frontend && npm audit --omit=dev --json 2>/dev/null || true)
    high=$(echo "$audit" | grep -o '"high":[0-9]*' | head -1 | cut -d: -f2)
    crit_n=$(echo "$audit" | grep -o '"critical":[0-9]*' | head -1 | cut -d: -f2)
    if [ "${crit_n:-0}" != "0" ]; then
        bad "npm audit: critical=$crit_n, high=${high:-0} (cd apps/frontend && npm audit)"
    elif [ "${high:-0}" != "0" ]; then
        wrn "npm audit: high=${high:-0} (прод-зависимости фронта)"
    else
        ok "npm audit: критичных и high в прод-зависимостях нет"
    fi
else
    wrn "npm недоступен — зависимости фронтенда НЕ проверены"
fi

echo "== Поверхность сети (Гл. 9.1: наружу только gateway и UI)"
exposed=$(grep -E '^\s+- "[0-9]+:' deployments/docker-compose.yml | wc -l)
ok "портов проброшено наружу в compose: $exposed (dev-стенд; в проде инфра — во внутренней сети, TLS-терминация на ingress — NFR-SEC-01)"

echo
if [ "$crit" -eq 0 ]; then
    printf '\033[32m>> критичных находок нет\033[0m (warn: %d)\n' "$warn"
    exit 0
fi
printf '\033[31m>> КРИТИЧНЫХ НАХОДОК: %d\033[0m (warn: %d) — см. docs/security-review.md\n' \
    "$crit" "$warn"
exit 1
