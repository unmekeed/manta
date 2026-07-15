# Chapter 7. API Design and Interface Specifications

## 7.1. API design principles

| Principle | Implementation |
|---|---|
| Resource model | REST for external clients, plural resource names |
| Versioning | `/api/v1` prefix; breaking changes → new major version |
| Unified error format | RFC 7807 (Problem Details) |
| Idempotency | `Idempotency-Key` header for POST mutations |
| Pagination | cursor-based (`cursor`, `limit`) |
| Authentication | Bearer JWT (see Ch. 9) |
| Rate limiting | `X-RateLimit-*` headers |
| Tracing | `traceparent` (W3C Trace Context) |
| Internal calls | gRPC over HTTP/2 + mTLS |

---

## 7.2. REST API Specification (OpenAPI fragment)

```yaml
openapi: 3.0.3
info:
  title: Dota AI Analyst Core API
  version: 2.0.0
paths:
  /api/v1/matches/upload:
    post:
      summary: Upload a binary replay file (.dem) for express analysis
      operationId: uploadReplay
      requestBody:
        required: true
        content:
          multipart/form-data:
            schema:
              type: object
              properties:
                file:
                  type: string
                  format: binary
                user_id:
                  type: string
                  format: uuid
      responses:
        '202':
          description: File accepted and queued for ETL processing
          content:
            application/json:
              schema:
                type: object
                properties:
                  job_id:
                    type: string
                    format: uuid
                  estimated_time_seconds:
                    type: integer
                    example: 10
        '400':
          description: Invalid file format or corrupted header structure
        '401':
          description: Access token authorization error
```

The full contract is provided in a separate file: [openapi/dota-ai-analyst.yaml](../openapi/dota-ai-analyst.yaml).

---

## 7.3. REST endpoint catalog

### 7.3.1. Matches and analysis

| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/api/v1/matches/upload` | Upload replay | Bearer |
| GET | `/api/v1/matches/{id}` | Match metadata | Bearer |
| GET | `/api/v1/matches/{id}/analysis` | Analysis result | Bearer |
| GET | `/api/v1/matches/{id}/timeline` | WP/net worth time series | Bearer |
| GET | `/api/v1/matches/{id}/heatmap` | Heatmap data | Bearer |
| GET | `/api/v1/jobs/{job_id}` | Job status | Bearer |

### 7.3.2. Players and profiles

| Method | Path | Purpose | Auth |
|---|---|---|---|
| GET | `/api/v1/players/{id}/profile` | Player profile | Bearer |
| GET | `/api/v1/players/{id}/metrics` | Aggregated metrics | Bearer |
| GET | `/api/v1/players/{id}/errors` | Error history | Premium |
| GET | `/api/v1/players/{id}/training-plan` | Training plan | Premium |

### 7.3.3. Draft, similarity, meta

| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/api/v1/draft/simulate` | Draft simulation | Bearer |
| POST | `/api/v1/similarity/search` | Search similar matches/players | Bearer |
| GET | `/api/v1/meta/heroes` | Hero meta (win rates) | public |
| GET | `/api/v1/meta/trends` | Meta trends | public |

### 7.3.4. Live (WebSocket)

| Protocol | Path | Purpose |
|---|---|---|
| WS | `/api/v1/live/{match_id}` | Win Probability stream |
| WS | `/api/v1/live/draft/{session_id}` | Live draft simulation |

---

## 7.4. Response data models (examples)

### 7.4.1. Match analysis result

```json
{
  "match_id": 7654321098,
  "status": "completed",
  "win_probability": {
    "final_radiant": 0.73,
    "timeline": [
      { "game_time": 0, "radiant": 0.50 },
      { "game_time": 600, "radiant": 0.58 },
      { "game_time": 1500, "radiant": 0.71 }
    ]
  },
  "players": [
    {
      "player_id": 12345,
      "hero_id": 8,
      "laning_score": 0.81,
      "impact_score": 0.144,
      "errors": [
        {
          "type": "positional_failure",
          "game_time": 1320,
          "delta_wp": -0.061,
          "safety_index": 0.87,
          "explanation": "Death in fog of war without vision near enemies."
        }
      ]
    }
  ],
  "narrative": "AI Coach: the team dominated the mid game...",
  "partial": false
}
```

### 7.4.2. Draft simulation request

```json
{
  "patch": "7.36c",
  "radiant_picks": [8, 74],
  "dire_picks": [1, 35],
  "bans": [86, 41],
  "next_action": "radiant_pick"
}
```

Response:

```json
{
  "predicted_winrate_radiant": 0.54,
  "recommendations": [
    { "hero_id": 26, "expected_winrate": 0.58, "reason": "synergy + counter to Dire" },
    { "hero_id": 5,  "expected_winrate": 0.56, "reason": "laning boost" }
  ]
}
```

---

## 7.5. Unified error format (RFC 7807)

```json
{
  "type": "https://api.dota-ai-analyst.com/errors/invalid-replay",
  "title": "Invalid replay file",
  "status": 400,
  "detail": "The file is not a valid Source 2 Demo (.dem).",
  "instance": "/api/v1/matches/upload",
  "trace_id": "9f8e7d6c-..."
}
```

### 7.5.1. Error code table

| HTTP | Code (type) | Meaning |
|---|---|---|
| 400 | `invalid-replay` | Malformed/corrupted file |
| 401 | `unauthorized` | Missing/invalid token |
| 403 | `forbidden` | Insufficient rights (RBAC) |
| 404 | `not-found` | Resource not found |
| 409 | `duplicate-job` | Idempotency conflict |
| 422 | `validation-error` | Field validation error |
| 429 | `rate-limited` | Request limit exceeded |
| 500 | `internal-error` | Internal error |
| 503 | `service-unavailable` | Downstream unavailable |

---

## 7.6. Internal gRPC contracts

Full `.proto` files are in the [proto directory](../proto/). Service overview:

```proto
service MLService {
  rpc Predict(PredictRequest) returns (PredictResponse);
  rpc PredictStream(stream FeatureFrame) returns (stream WinProbability);
}

service SimilarityService {
  rpc FindSimilar(SimilarityQuery) returns (SimilarityResult);
}

service DraftService {
  rpc SimulateDraft(DraftState) returns (DraftRecommendation);
}

service FeatureStore {
  rpc GetOnlineFeatures(FeatureRequest) returns (FeatureVector);
  rpc WriteFeatures(FeatureBatch) returns (WriteAck);
}
```

### 7.6.1. REST → gRPC mapping

| REST endpoint | Internal gRPC call |
|---|---|
| `GET /matches/{id}/analysis` | `MLService.Predict` + `LLM` (async) |
| `POST /draft/simulate` | `DraftService.SimulateDraft` |
| `POST /similarity/search` | `SimilarityService.FindSimilar` |
| `GET /players/{id}/training-plan` | `RecommendationService.BuildPlan` |
| `WS /live/{match_id}` | `MLService.PredictStream` |

---

## 7.7. Versioning, compatibility and deprecation

| Aspect | Policy |
|---|---|
| API version | in the path (`/api/v1`); v2 on breaking changes |
| gRPC/Avro compatibility | `BACKWARD` (optional fields may be added) |
| Deprecation | `Deprecation` + `Sunset` headers, minimum 6 months |
| Change detection | contract tests in CI (see Ch. 10) |

---

## 7.8. Rate limits

| Tier | Limit (requests/min) | Replay uploads/day |
|---|---|---|
| Free | 60 | 5 |
| Premium | 300 | 100 |
| Pro (B2B) | 1200 | 2000 |
| Internal | unlimited | unlimited |

Response headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
On exceedance — `429` + `Retry-After`.
