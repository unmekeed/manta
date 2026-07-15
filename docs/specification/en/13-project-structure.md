# Chapter 13. Project Structure Down to Files and Modules

## 13.1. Repository strategy

The project is organized as a **monorepo** with clear separation into applications (`apps/`), shared
libraries (`libs/`), infrastructure (`deployments/`, `infra/`) and documentation (`docs/`). This
approach simplifies atomic changes to cross-service contracts and a unified CI.

| Principle | Implementation |
|---|---|
| Service isolation | each service is a self-contained deploy unit in `apps/` |
| Reuse | shared contracts and utilities in `libs/` and `proto/` |
| Unified contracts | `proto/` and `openapi/` as the source of truth |
| Infra alongside code | `deployments/`, `infra/` versioned with code |
| Unified CI | `.github/workflows/` with a per-service matrix |

---

## 13.2. Monorepo root tree

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
│   ├── proto/                  # generated gRPC stubs
│   ├── py-common/              # shared Python utilities
│   ├── go-common/              # shared Go packages
│   └── schemas/                # Avro/JSON Kafka schemas
├── proto/                      # source .proto (source of truth)
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

## 13.3. Structure of key services

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
│   ├── clients/            # gRPC clients to internal services
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
├── go/                     # wrapper and gRPC/Kafka
│   ├── worker.go
│   └── kafka_consumer.go
├── tests/
│   ├── fixtures/           # reference .dem files
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
│   ├── app.py                 # gRPC server
│   ├── predictors/
│   │   ├── win_probability.py
│   │   ├── laning.py
│   │   ├── draft_gnn.py
│   │   └── error_detection.py
│   ├── registry/              # MLflow integration
│   ├── features/              # Feature Store client
│   ├── serving/               # batching, cache, calibration
│   └── explain/               # SHAP explanations
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
│   │   └── ws.ts              # WebSocket client
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

## 13.4. Shared libraries

| Library | Language | Content |
|---|---|---|
| `libs/proto` | gen | generated gRPC stubs (Go/Python/TS) |
| `libs/schemas` | Avro/JSON | Kafka event schemas + versions |
| `libs/py-common` | Python | logging, tracing, config, Kafka helpers |
| `libs/go-common` | Go | middleware, telemetry, errors |

---

## 13.5. Code conventions and standards

| Area | Standard |
|---|---|
| Go | `gofmt`, `golangci-lint`, `cmd/internal/pkg` layout |
| Python | `ruff`/`black`, type hints, `pyproject.toml` |
| TypeScript | ESLint + Prettier, strict mode |
| C++ | `clang-format`, C++17, RAII |
| Service naming | kebab-case directories, domain in the name |
| Commits | Conventional Commits |
| Branching | trunk-based + short-lived branches |

---

## 13.6. Service-to-directory mapping

| Service (Ch. 3) | Directory | Language | Artifact |
|---|---|---|---|
| API Gateway | `apps/api-gateway` | Go | image + binary |
| Data Collector | `apps/data-collector` | Python/Go | image |
| Replay Parser | `apps/replay-parser` | C++/Go | image |
| ETL Service | `apps/etl-service` | Python | image |
| Feature Store | `apps/feature-store` | Python | image |
| ML Service | `apps/ml-service` | Python | image + models |
| LLM Service | `apps/llm-service` | Python | image |
| Recommendation | `apps/recommendation-engine` | Python | image |
| Draft Engine | `apps/draft-engine` | Go/Python | image |
| Meta Engine | `apps/meta-engine` | Python | image |
| Similarity Engine | `apps/similarity-engine` | Python | image |
| Frontend | `apps/frontend` | TS/React | static + Nginx image |

---

## 13.7. Makefile (root targets)

| Target | Action |
|---|---|
| `make lint` | linters across all services |
| `make test` | unit tests |
| `make contract-test` | contract tests (proto/OpenAPI) |
| `make proto` | generate gRPC stubs from `proto/` |
| `make build` | build images |
| `make up` | local run via docker-compose |
| `make security-scan` | SAST/SCA/secret scans |
