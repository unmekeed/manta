# Manta — Platform Monorepo

Реализация интеллектуальной платформы анализа матчей Dota 2 по
[спецификации v2.0.0](docs/specification/ru/README.md).

## Статус разработки (Roadmap Гл. 14)

| Фаза | Состояние | Содержание |
|---|---|---|
| **Фаза 1: Инфраструктура** | ✅ завершена (спринты 1–4) | compose-инфраструктура, миграции PG/CH, Kafka-топики, API Gateway (S3+outbox), Data Collector |
| **Фаза 2: Парсинг и ETL** | ✅ завершена (спринты 5–9) | Replay Parser (C++, полный декодер сущностей) + Go-обвязка, ClickHouse-слой сырых событий, Feature Extractor |
| **Фаза 3: Аналитика и ML** | ✅ завершена (спринты 10–48) | Win Probability (релизные критерии B1+B2 пройдены), Death-Risk модель, Similarity/Draft/Coach/Feature Store; Laning-модель — единственный открытый пункт (спринт 50) |
| **Фаза 4: UI, MLOps, Релиз** | 🟡 почти завершена | Frontend, gRPC-инференс, MLflow (постгрес-бэкенд), PSI-дрейф — готовы; публичный релиз WP (B4) ждёт решения владельца, нагрузочные тесты и security review (D5/D6) — гейты финализации |

### Что уже работает (проверено против живой инфраструктуры)

Полный конвейер: **OpenDota/загрузка → MinIO → Kafka → C++ парсер →
ClickHouse → фичи → модели (Win Probability, Death-Risk) → отчёты с
разбором ошибок → веб-UI** — на реальных матчах, включая про-эталон.

- `deployments/docker-compose.yml` — PostgreSQL 16, ClickHouse 24.8, Kafka 3.8 (KRaft), Redis 7, MinIO, MLflow (backend в postgres); все с healthcheck.
- `infra/migrations/` — реляционная схема Гл. 4.2, аналитический слой Гл. 4.4 (ReplayEvents, EconomyTimeline, PositionSnapshots) и витрины фич (PlayerMatchFeatures, MatchTimelineFeatures).
- `apps/api-gateway` — Go: upload → MinIO + outbox → Kafka; статусы AnalysisJob по `replay.parsed`/`dlq.parser`; REST `/matches`, `/timeline`, `/analysis`, `/heroes`, `/draft/simulate`; RFC 7807, trace_id, rate limit.
- `apps/data-collector` — Python, 4 источника (реплейный OpenDota + гибридный JSON-путь `opendota-timeline`/`-pro`): дедуп/курсор в PG, 429-бэкoff до сброса квоты, авто-reconnect к Postgres. **Шардирование между машинами** (`COLLECTOR_SHARD_COUNT`/`COLLECTOR_SHARD_ID`) — см. ниже.
- `apps/replay-parser` — C++17-ядро (битово-совместимый с `dotabuff/manta` декодер сущностей: позиции, экономика, combat log; 110 МиБ за ~4 с) + Go-сервис `svc/` (Kafka → ядро → ClickHouse → `replay.parsed`, DLQ).
- `apps/feature-extractor` — Python: `replay.parsed` → point-in-time фичи (GPM/XPM, LH/DN, alive/towers/rax diff, networth_rel) → витрины + `features.calculated`; пушит срез в Feature Store.
- `apps/ml-service` — gRPC :50051 (Predict/PredictStream); Win Probability (LightGBM + OOF-изотоника, Brier ≈ 0.14) и Death-Risk (P смерти за 30с, AUC 0.79) из общего реестра моделей (S3/MLflow).
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

Если среда разработки перезапустилась (эфемерный контейнер: dockerd и все
фоновые процессы погибли, данные в volumes целы) — весь стек поднимается
одной командой:

```bash
MANTA_TRAIN_ENV=~/manta-train.env make recover   # идемпотентно
```

`scripts/dev-recover.sh` запускает dockerd, инфраструктуру, парсер,
экстрактор, коллектор и auto-train (env-файл с Telegram-секретами — вне
репозитория); живые компоненты не трогает.

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
`dataset-import` конфликт-фри. Замечание: `dataset-export` НЕ переносит
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

1. **Спринт 49 — надёжность**: идемпотентные `make topics`/`make migrate` внутри recover, `make doctor` (health-check по данным, не по pgrep), Telegram-алерт «реплейный путь стоит», логи вне `/tmp`.
2. **Спринт 50 — Laning-модель** (вторая половина C5): обучаемая оценка лейнинга, каркас — `training/risk.py`.
3. **A9/A10**: даунвейт старого патча, Roshan/aegis/buyback/hero-фичи + Optuna (после 5000+ матчей).
4. **B4**: публичный релиз Win Probability — критерии выполнены, решение за владельцем.
5. **D5/D6**: нагрузочные тесты и security review — гейты Фазы 4.
6. **E1**: автозапуск стека на Windows (Планировщик задач + `wsl … make recover`) и ежедневный `dataset-export` как бэкап.
