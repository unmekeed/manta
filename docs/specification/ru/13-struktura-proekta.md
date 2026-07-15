# Глава 13. Структура проекта до уровня файлов и модулей

## 13.1. Стратегия репозитория

Проект организован как **монорепозиторий** с чётким разделением на приложения (`apps/`),
разделяемые библиотеки (`libs/`), инфраструктуру (`deployments/`, `infra/`) и документацию
(`docs/`). Такой подход упрощает атомарные изменения контрактов между сервисами и единый CI.

| Принцип | Реализация |
|---|---|
| Изоляция сервисов | каждый сервис — самостоятельный деплой-юнит в `apps/` |
| Переиспользование | общие контракты и утилиты в `libs/` и `proto/` |
| Единые контракты | `proto/` и `openapi/` как источник истины |
| Инфраструктура рядом | `deployments/`, `infra/` версионируются с кодом |
| Единый CI | `.github/workflows/` с матрицей по сервисам |

---

## 13.2. Корневое дерево монорепозитория

```
dota-ai-analyst/
├── .github/
│   └── workflows/
│       ├── ci-cd-pipeline.yml
│       ├── security-scan.yml
│       └── ml-retrain.yml
├── apps/
│   ├── api-gateway/
│   ├── data-collector/
│   ├── replay-parser/
│   ├── etl-service/
│   ├── feature-store/
│   ├── ml-service/
│   ├── llm-service/
│   ├── recommendation-engine/
│   ├── draft-engine/
│   ├── meta-engine/
│   ├── similarity-engine/
│   └── frontend/
├── libs/
│   ├── proto/                  # сгенерированные gRPC-стабы
│   ├── py-common/              # общие Python-утилиты
│   ├── go-common/              # общие Go-пакеты
│   └── schemas/                # Avro/JSON-схемы Kafka
├── proto/                      # исходные .proto (источник истины)
├── openapi/
│   └── dota-ai-analyst.yaml
├── deployments/
│   ├── docker-compose.yml
│   ├── helm/
│   └── kubernetes/
│       ├── deployment-ml.yaml
│       └── hpa-parser.yaml
├── infra/
│   └── terraform/
├── docs/
│   ├── specification/
│   └── adr/
├── Makefile
└── README.md
```

---

## 13.3. Структура ключевых сервисов

### 13.3.1. API Gateway (Go)

```
apps/api-gateway/
├── cmd/
│   └── server/
│       └── main.go
├── internal/
│   ├── config/
│   ├── middleware/
│   │   ├── auth.go
│   │   ├── ratelimit.go
│   │   └── tracing.go
│   ├── handlers/
│   │   ├── matches.go
│   │   ├── players.go
│   │   ├── draft.go
│   │   └── live_ws.go
│   ├── clients/            # gRPC-клиенты к внутренним сервисам
│   └── router/
├── pkg/
├── Dockerfile
├── go.mod
└── go.sum
```

### 13.3.2. Replay Parser (C++/Go)

```
apps/replay-parser/
├── include/
│   ├── parser_core.hpp
│   ├── demo_reader.hpp
│   ├── entity_decoder.hpp
│   └── event_extractor.hpp
├── src/
│   ├── parser_core.cpp
│   ├── demo_reader.cpp
│   ├── entity_decoder.cpp
│   ├── string_tables.cpp
│   ├── event_extractor.cpp
│   └── serializer.cpp
├── go/                     # обвязка и gRPC/Kafka
│   ├── worker.go
│   └── kafka_consumer.go
├── tests/
│   ├── fixtures/           # эталонные .dem
│   └── parser_test.cpp
├── CMakeLists.txt
└── Dockerfile
```

### 13.3.3. ML Service (Python)

```
apps/ml-service/
├── models/
│   ├── laning_xgboost.pkl
│   ├── draft_gnn.pt
│   ├── win_probability.pkl
│   └── error_detection.pkl
├── src/
│   ├── app.py                 # gRPC-сервер
│   ├── predictors/
│   │   ├── win_probability.py
│   │   ├── laning.py
│   │   ├── draft_gnn.py
│   │   └── error_detection.py
│   ├── registry/              # интеграция с MLflow
│   ├── features/              # клиент Feature Store
│   ├── serving/               # батчинг, кэш, калибровка
│   └── explain/               # SHAP-объяснения
├── tests/
│   ├── test_predictors.py
│   └── test_calibration.py
├── requirements.txt
└── Dockerfile
```

### 13.3.4. Frontend (React + TypeScript)

```
apps/frontend/
├── src/
│   ├── components/
│   │   ├── HeatmapCanvas.tsx
│   │   ├── DraftSimulator.tsx
│   │   ├── WinProbabilityChart.tsx
│   │   ├── RadarProfile.tsx
│   │   └── TimelineScrubber.tsx
│   ├── pages/
│   │   ├── MatchAnalysisPage.tsx
│   │   ├── DraftSimulatorPage.tsx
│   │   ├── PlayerProfilePage.tsx
│   │   └── MetaDashboardPage.tsx
│   ├── store/
│   │   ├── useMatchStore.ts
│   │   ├── useDraftStore.ts
│   │   └── useAuthStore.ts
│   ├── api/
│   │   ├── client.ts          # TanStack Query
│   │   └── ws.ts              # WebSocket-клиент
│   ├── hooks/
│   ├── workers/               # Web Workers (heatmap)
│   └── App.tsx
├── public/
├── tests/
│   ├── unit/
│   └── e2e/                   # Playwright
├── package.json
├── vite.config.ts
├── tsconfig.json
└── Dockerfile
```

### 13.3.5. ETL Service (Python)

```
apps/etl-service/
├── src/
│   ├── app.py
│   ├── consumers/
│   │   └── replay_parsed.py
│   ├── validation/
│   │   └── data_quality.py    # Great Expectations
│   ├── enrichment/
│   ├── aggregation/
│   │   └── windows.py
│   ├── sinks/
│   │   ├── clickhouse.py
│   │   └── postgres.py
│   └── outbox/
├── tests/
├── requirements.txt
└── Dockerfile
```

---

## 13.4. Разделяемые библиотеки

| Библиотека | Язык | Содержание |
|---|---|---|
| `libs/proto` | gen | сгенерированные gRPC-стабы (Go/Python/TS) |
| `libs/schemas` | Avro/JSON | схемы событий Kafka + версии |
| `libs/py-common` | Python | логирование, трейсинг, конфиг, Kafka-хелперы |
| `libs/go-common` | Go | middleware, телеметрия, ошибки |

---

## 13.5. Соглашения и стандарты кода

| Область | Стандарт |
|---|---|
| Go | `gofmt`, `golangci-lint`, layout `cmd/internal/pkg` |
| Python | `ruff`/`black`, type hints, `pyproject.toml` |
| TypeScript | ESLint + Prettier, strict mode |
| C++ | `clang-format`, C++17, RAII |
| Именование сервисов | kebab-case каталоги, домен в имени |
| Коммиты | Conventional Commits |
| Ветвление | trunk-based + короткоживущие ветки |

---

## 13.6. Соответствие сервисов и каталогов

| Сервис (Гл. 3) | Каталог | Язык | Артефакт |
|---|---|---|---|
| API Gateway | `apps/api-gateway` | Go | образ + бинарь |
| Data Collector | `apps/data-collector` | Python/Go | образ |
| Replay Parser | `apps/replay-parser` | C++/Go | образ |
| ETL Service | `apps/etl-service` | Python | образ |
| Feature Store | `apps/feature-store` | Python | образ |
| ML Service | `apps/ml-service` | Python | образ + модели |
| LLM Service | `apps/llm-service` | Python | образ |
| Recommendation | `apps/recommendation-engine` | Python | образ |
| Draft Engine | `apps/draft-engine` | Go/Python | образ |
| Meta Engine | `apps/meta-engine` | Python | образ |
| Similarity Engine | `apps/similarity-engine` | Python | образ |
| Frontend | `apps/frontend` | TS/React | статика + Nginx-образ |

---

## 13.7. Makefile (корневые цели)

| Цель | Действие |
|---|---|
| `make lint` | линтеры по всем сервисам |
| `make test` | unit-тесты |
| `make contract-test` | контрактные тесты (proto/OpenAPI) |
| `make proto` | генерация gRPC-стабов из `proto/` |
| `make build` | сборка образов |
| `make up` | локальный запуск через docker-compose |
| `make security-scan` | SAST/SCA/секрет-сканы |
