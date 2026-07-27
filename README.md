# Manta — Platform Monorepo

Реализация интеллектуальной платформы анализа матчей Dota 2 по
[спецификации v2.0.0](docs/specification/ru/README.md).

## Статус разработки (Roadmap Гл. 14)

| Фаза | Состояние | Содержание |
|---|---|---|
| **Фаза 1: Инфраструктура** | ✅ завершена (спринты 1–4) | compose-инфраструктура, миграции PG/CH, Kafka-топики, API Gateway (S3+outbox), Data Collector |
| **Фаза 2: Парсинг и ETL** | ✅ завершена (спринты 5–9) | Replay Parser (C++, полный декодер сущностей) + Go-обвязка, ClickHouse-слой сырых событий, Feature Extractor |
| **Фаза 3: Аналитика и ML** | ✅ завершена (спринты 10–48) | Win Probability (релизные критерии B1+B2 пройдены), Death-Risk модель, Similarity/Draft/Coach/Feature Store; Laning-модель — единственный открытый пункт (спринт 50) |
| **Фаза 4: UI, MLOps, Релиз** | 🟡 почти завершена | Frontend, gRPC-инференс, MLflow (постгрес-бэкенд), PSI-дрейф — готовы; нагрузочные тесты и security review (D5/D6) пройдены, JWT/RBAC+TLS+GDPR реализованы (спринт 58); публичный релиз WP (B4) ждёт решения владельца |

### Что уже работает (проверено против живой инфраструктуры)

Полный конвейер: **OpenDota/загрузка → MinIO → Kafka → C++ парсер →
ClickHouse → фичи → модели (Win Probability, Death-Risk) → отчёты с
разбором ошибок → веб-UI** — на реальных матчах, включая про-эталон.

- `deployments/docker-compose.yml` — PostgreSQL 16, ClickHouse 24.8, Kafka 3.8 (KRaft), Redis 7, MinIO, MLflow (backend в postgres); все с healthcheck.
- `infra/migrations/` — реляционная схема Гл. 4.2, аналитический слой Гл. 4.4 (ReplayEvents, EconomyTimeline, PositionSnapshots) пер-матчевые MatchDraft/MatchEvents и витрины фич (PlayerMatchFeatures, MatchTimelineFeatures).
- `apps/api-gateway` — Go: upload → MinIO + outbox → Kafka; статусы AnalysisJob по `replay.parsed`/`dlq.parser`; REST `/matches`, `/timeline`, `/analysis`, `/heroes`, `/draft/simulate`; RFC 7807, trace_id, rate limit.
- `apps/data-collector` — Python, 4 источника (реплейный OpenDota + гибридный JSON-путь `opendota-timeline`/`-pro`): дедуп/курсор в PG, 429-бэкoff до сброса квоты, авто-reconnect к Postgres. **Шардирование между машинами** (`COLLECTOR_SHARD_COUNT`/`COLLECTOR_SHARD_ID`) — см. ниже. Из скачанного JSON извлекаются драфт, события (Рошан/аегис/FB/бэйбеки/руны/варды) и 12 поминутных фич (`collector/signals.py`), сам JSON складывается в MinIO (`collector/rawstore.py`) — чтобы будущие фичи бэкфиллились без квоты.
- `apps/replay-parser` — C++17-ядро (битово-совместимый с `dotabuff/manta` декодер сущностей: позиции, экономика, combat log; 110 МиБ за ~4 с) + Go-сервис `svc/` (Kafka → ядро → ClickHouse → `replay.parsed`, DLQ).
- `apps/feature-extractor` — Python: `replay.parsed` → point-in-time фичи (GPM/XPM, LH/DN, alive/towers/rax diff, networth_rel) → витрины + `features.calculated`; пушит срез в Feature Store.
- `apps/ml-service` — gRPC :50051 (Predict/PredictStream); Win Probability (LightGBM + OOF-изотоника, 22 фичи, Brier ≈ 0.14), Death-Risk (P смерти за 30с, AUC 0.79), Laning (P выиграть линию по игре первых 5 минут, AUC 0.89) и Draft Prior (P(win\|составы), AUC 0.585) из общего реестра моделей (S3/MLflow).
- `apps/report-generator` — WP-кривая, SHAP-атрибуция ошибок, модельный Safety Index, позиции смертей → MatchReports.
- `apps/similarity`, `apps/draft`, `apps/coach`, `apps/feature-store` — gRPC-сервисы (:50052–:50055) поверх витрин/эмбеддингов.
- `apps/frontend` (React+TS+Vite) — список матчей, страница матча (WP-бейдж, SHAP-чипы, карта смертей, риск-бейджи), драфт-симулятор.

## Быстрый старт

```bash
make up        # поднять инфраструктуру
make migrate   # применить миграции PG + CH
make topics    # создать Kafka-топики

# запустить шлюз
cd apps/api-gateway && go run ./cmd/server

# проверить
curl localhost:8080/healthz
curl -X POST localhost:8080/api/v1/matches/upload -F "file=@replay.dem"
```

Весь стек — от инфраструктуры до веб-интерфейса — поднимается одной
командой (идемпотентно: живые компоненты не трогает, безопасно запускать
повторно и после любого перезапуска среды/ПК):

```bash
MANTA_TRAIN_ENV=~/manta-train.env make recover
```

`scripts/dev-recover.sh` запускает: dockerd → инфраструктуру (PG, CH,
Kafka, MinIO, Redis) → топики и миграции (идемпотентно, журнал
`SchemaMigrations`) → парсер, экстрактор, коллекторы, ml-service,
similarity/draft/coach/feature-store, report-generator, auto-train →
**api-gateway (:8080), веб-интерфейс (:5173) и дашборд (:9107)** — и
заканчивается health-check'ом `make doctor`. Env-файл с секретами — вне
репозитория; логи сервисов — `~/manta-logs` (`MANTA_LOG_DIR`), не `/tmp`.

После recover открывать: веб-интерфейс http://localhost:5173, живой
дашборд http://localhost:9107.

### Панель обучения (:9107)

Раздел «Обучение модели» показывает то, ради чего дашборд открывают во
время сбора:

- **фазовые Brier** (early / mid / late) плюс valid и про-эталон —
  агрегат маскирует, что поздняя игра тривиальна, а ранняя как раз там,
  где модель слаба;
- **рост датасета по дням** за 30 дней — пустой столбец сразу виден, это
  день, когда сбор стоял;
- **дрейф фич (PSI)**, худшие сверху: большие значения означают, что
  модель обучена не на том, что приходит сейчас;
- **таблица ablation** — какая группа фич заслужила место (Δ = Brier без
  фичи минус Brier со всеми, Δ>0 → фича полезна). Заполняется кнопкой
  «Ablation фич».

Данные берутся из уже опрашиваемых источников: Prometheus auto-train,
ClickHouse и JSON-отчёта ablation. Читать артефакт модели напрямую было
бы свежее, но потянуло бы lightgbm в дашборд, который намеренно живёт на
голой стандартной библиотеке и обязан подниматься, когда всё лежит.

### Панель управления (:9107)

Дашборд не только показывает телеметрию, но и запускает конвейер:
кнопки «Поднять всё», «Проверить», «Статус обучения», «Переобучить WP»,
«Ablation фич», «Остановить сервисы» — с живым логом прямо на странице.

Страница, умеющая выполнять команды, — это удалённое выполнение кода,
поэтому защита из трёх слоёв:

- слушает **127.0.0.1** (`DASHBOARD_BIND`); если бинд не петлевой,
  действия отключаются, пока не задан `DASHBOARD_ALLOW_REMOTE_ACTIONS=1`;
- POST требует заголовок `X-Manta-Action: 1` — без него чужая открытая
  в браузере страница могла бы отправить форму на localhost и остановить
  сбор (браузер не пошлёт нестандартный заголовок кросс-доменно);
- действия — фиксированный список, аргументы зашиты в коде; поля
  «введите команду» нет.

Из Windows панель доступна как обычно по `localhost` — проброс WSL2
работает и для 127.0.0.1.

**`make stop` дашборд НЕ останавливает** — иначе кнопка «Поднять всё»
исчезла бы вместе с сервисами. Остановить его отдельно:
`pkill -f scripts/dashboard.py`. Первый запуск на свежей машине —
по-прежнему `make recover` из терминала или задача автозапуска.

**Обновление кода**: recover не перезапускает уже живые процессы (`if !
pgrep`), поэтому после `git pull` они останутся на старом коде — всё
«up», а нового поведения нет. Штатный порядок (подробности —
`docs/runbooks.md` §5):

```bash
make stop        # только хостовые процессы; контейнеры и данные не трогаются
git pull origin main
make migrate
MANTA_TRAIN_ENV=~/manta-train.env make recover
make doctor
```

Проверить здоровье конвейера в любой момент — по ДАННЫМ, а не по процессам
(топики, лаг консьюмер-групп, свежесть таблиц, квота OpenDota, часы,
миграции):

```bash
make doctor
```

## Безопасность: JWT, TLS, GDPR

По умолчанию на локальном стенде gateway слушает HTTP и пускает без
токенов (в логе — WARN `auth_disabled`/`tls_disabled`). Включить:

```bash
./scripts/gen-dev-keys.sh            # ключи RS256 + самоподписанный TLS
# прописать выведенные пути в ~/manta-train.env, затем make recover
```

После этого API работает по HTTPS (TLS 1.3, версии ниже отвергаются), а
закрытые эндпоинты требуют Bearer-токен. Роли Гл. 9.3: `anonymous` —
матчи/разборы/герои/драфт, `free` — загрузка реплеев, `premium` —
GDPR-экспорт, `admin` — выпуск токенов и стирание данных.

```bash
./scripts/issue-token.sh admin       # первый токен (локально, ключом)
curl -sk https://localhost:8080/.well-known/jwks.json
curl -sk -X POST https://localhost:8080/api/v1/auth/token \
     -H "Authorization: Bearer $ADMIN" \
     -d '{"sub":"user-1","role":"premium"}'
```

GDPR (Гл. 9.7): `GET /api/v1/players/{ник}/export` — все данные субъекта
одним JSON; `DELETE /api/v1/players/{ник}/data` — стирание никнейма из
витрины и отчётов (игровая статистика остаётся обезличенной). Субъект
идентифицируется никнеймом: account_id платформа не хранит.

**Псевдонимизация никнеймов** (спринт 70, по умолчанию выключена):

```bash
MANTA_PII_MODE=pseudonymize
MANTA_PII_SALT=$(cat ~/manta-keys/pii-salt)   # создаёт gen-dev-keys.sh
```

В этом режиме витрина и отчёты хранят `player_hash =
HMAC-SHA256(соль, casefold(ник))[:16]` вместо самого ника. Обратной
таблицы нет: GDPR-операции работают вперёд (субъект называет ник — шлюз
его хеширует и ищет), а интерфейсу имя не нужно — он показывает героя.
Соль после начала сбора не менять: она связывает ник с уже записанными
псевдонимами. Подробности и открытые пункты — `docs/security-review.md`.

### mTLS между внутренними сервисами

По умолчанию gRPC-сервисы (:50051–:50055) слушают открыто и пишут в лог
WARN `mtls_disabled`. Включить — задать все три переменные (частичная
конфигурация намеренно оставляет режим insecure, а несуществующий путь
роняет сервис, а не откатывает в открытый режим):

```bash
MANTA_MTLS_CA_FILE=~/manta-keys/mtls-ca.pem
MANTA_MTLS_CERT_FILE=~/manta-keys/mtls-cert.pem
MANTA_MTLS_KEY_FILE=~/manta-keys/mtls-key.pem
```

Сервер требует сертификат клиента (`require_client_auth=True`), поэтому
посторонний процесс не подключится к ML Service, даже находясь на той же
машине. Обвязка — `libs/manta_grpc`, тесты поднимают настоящий сервер и
проверяют отказ клиенту без сертификата и с сертификатом чужого CA.

### Retention отчётов и раздельные роли БД

Отчёты содержат PII и растут без ограничения. Чистка выключена по
умолчанию; включается сроком хранения, а CLI по умолчанию только
показывает, что удалилось бы:

```bash
python -m reportgen.retention                     # сухой прогон
python -m reportgen.retention --days 180 --apply  # удалить
# в env-файле: REPORTS_RETENTION_DAYS=180 — report-generator чистит раз в сутки
```

Отчёт — производный артефакт: исходные данные остаются в ClickHouse, и
он пересоздаётся через `python -m reportgen --match ID`.

Все сервисы ходят в Postgres под одним `dota` с полными правами. Миграция
005 создаёт роли по функции, скрипт — пользователей для входа:

```bash
MANTA_DB_PASS_COLLECTOR=$(openssl rand -hex 16) \
MANTA_DB_PASS_REPORTS=$(openssl rand -hex 16) \
MANTA_DB_PASS_GATEWAY=$(openssl rand -hex 16) \
  ./scripts/create-db-users.sh
```

Дальше — заменить `POSTGRES_DSN` у соответствующих сервисов. Коллектор,
самый «внешний» сервис, получает права только на две свои таблицы и не
может писать в отчёты. `dota` не тронут: откат — возврат прежнего DSN.

## Синхронизация датасета между машинами

Датасет собирается независимо на каждой машине (облако, локалка) и
расходится. Перенос — одной командой в каждую сторону:

```bash
make dataset-export                       # → manta-dataset-<дата>.tar
make dataset-import IN=manta-dataset-….tar  # идемпотентно, повторять можно
```

Переносятся витрины ClickHouse (Replacing-дедуп), сырьё позиций/экономики
(вливаются только новые match_id), дедуп коллекторов и готовые отчёты
(побеждает более свежий `generated_at`). Подробности — в шапке
`scripts/dataset-sync.sh`.

### Параллельный сбор на нескольких машинах (разные IP)

Квота OpenDota считается по IP (~3000 запросов/сутки анонимно). Две
машины с разными IP удваивают сбор — но, читая один список матчей, схватят
одни и те же. Шардирование по `match_id % N` разводит их без координации:

```bash
# в env-файле каждой машины (MANTA_TRAIN_ENV), COUNT одинаков, ID разный:
#   ПК №1:  COLLECTOR_SHARD_COUNT=2   COLLECTOR_SHARD_ID=0   # чётные
#   ПК №2:  COLLECTOR_SHARD_COUNT=2   COLLECTOR_SHARD_ID=1   # нечётные
```

Множества собранных матчей не пересекаются, поэтому слияние баз через
`dataset-import` конфликт-фри.

### Бэкапы и автозапуск (Windows)

docker-volume — единственная копия датасета, и он уже терялся. Слепок с
ротацией (по умолчанию 7 дней, каталог `~/manta-backups`):

```bash
make backup                                  # разово
MANTA_BACKUP_DIR=/mnt/d/manta-backups make backup   # на диск Windows
```

Каталог на `/mnt/c`|`/mnt/d` переживает `wsl --unregister`. Сбой бэкапа
шлёт сообщение в Telegram (молча сломавшийся бэкап хуже отсутствующего);
старые слепки удаляются только после успешного нового.

Автозапуск: из PowerShell **от администратора** (пути — свои):

```powershell
powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\<user>\manta\scripts\autostart-install.ps1
```

Создаст две задачи Планировщика: `Manta-Recover` (при входе в систему —
Docker Desktop, ожидание демона, `make recover`) и `Manta-Backup`
(ежедневно). Удалить: тот же скрипт с `-Uninstall`. Замечание: `dataset-export` НЕ переносит
`ReplayEvents` (combat-лог, под TTL) — для Death-Risk на объединённом
датасете нужно, чтобы реплеи парсились на той же машине, где потом
обучаешь, либо расширить экспорт.

## Наблюдаемость без Docker/Grafana

Каждый сервис отдаёт Prometheus-метрики на своём порту:

| Порт | Сервис | Порт | Сервис |
|---|---|---|---|
| `9101` | parser-svc | `9104` | ml-service (gRPC) |
| `9102` | feature-extractor | `9105` | data-collector |
| `9103` | report-generator | `9106` | ml-autotrain |

Посмотреть, что реально слушает порты: `sudo ss -tlnp`. Сырые метрики
сервиса: `curl -s localhost:9106/metrics`.

Живой дашборд без установки чего-либо (только python3) — собирает метрики
всех сервисов серверно (обходит CORS браузера), плюс число матчей прямо из
ClickHouse; авто-обновление, спарклайны, статус up/down, тёмная/светлая тема:

```bash
make dashboard        # http://localhost:9107
```

`scripts/dashboard.py` — один файл на стандартной библиотеке; порты можно
переопределить (`DASHBOARD_PORT`, `*_METRICS_PORT`). Инфраструктурные порты:
ClickHouse `8123` (HTTP) / `9000` (native), Kafka `9092`, Postgres `5432`,
Redis `6379`, MinIO `9500` (S3) / `9501` (веб-консоль).

## Структура

Соответствует Гл. 13 спецификации: `apps/` (12 сервисов), `libs/` (общие схемы и библиотеки),
`proto/` + `openapi/` (контракты — источник истины), `infra/` (миграции, топики, terraform),
`deployments/` (compose, helm, k8s).

- `apps/replay-parser` — C++17-ядро: DemoReader (mmap, покадровая итерация, snappy), pb_lite (protobuf wire-формат без protoc), разбор CDemoFileHeader/CDemoFileInfo, CLI `demoinfo`; unit-тесты на синтетическом `.dem`. Реальный реплей 8892914077 (110.6 МиБ) читается за 62 мс; файл-эталон в dev-MinIO `s3://replays/fixtures/8892914077.dem`.

## Следующие спринты

Полный план и обоснование — `docs/ROADMAP.md`, живой контекст с историей
инцидентов — `docs/HANDOFF.md`. Актуальный порядок:

Треки A, C, D, E закрыты (спринты 1–58); трек F (усиление модели,
`docs/ML-PLAN.md`) реализован в спринтах 60–66.

1. **Ревизия трека F — нужны данные, не код.** 12 новых фич (объективы,
   предметы, варды, руны, нейтралки, уровни) извлекаются в момент сбора,
   поэтому на 2635 исторических матчах они NaN и эффект пока не измерим.
   Нужно ~2–3 тысячи новых матчей, затем переобучение и фазовое
   сравнение; фичи без эффекта — выкинуть (правило ML-PLAN §6). Честные
   результаты на сегодня — ML-PLAN §8: draft prior выигрыша не дал,
   Optuna ниже порога значимости.
2. **F8** (hero-эмбеддинги) — гейт 20 000 матчей, сейчас 2635.
3. **B4**: публичный релиз Win Probability — технические критерии B1+B2
   выполнены, часть гейтов безопасности снята в спринте 58; остаются
   mTLS, сертификат от CA, входы Steam/email, псевдонимизация PII
   (`docs/security-review.md` §4–6). Решение за владельцем.
4. ~~Обновить тулчейн Go до 1.25.12+~~ ✅ спринт 68: govulncheck 26/22 → 0/0.
