# Глава 7. Проектирование API и спецификации интерфейсов

## 7.1. Принципы проектирования API

| Принцип | Реализация |
|---|---|
| Ресурсная модель | REST для внешних клиентов, ресурсы во множественном числе |
| Версионирование | префикс `/api/v1`; breaking changes → новая мажорная версия |
| Единый формат ошибок | RFC 7807 (Problem Details) |
| Идемпотентность | заголовок `Idempotency-Key` для POST-мутаций |
| Пагинация | cursor-based (`cursor`, `limit`) |
| Аутентификация | Bearer JWT (см. Гл. 9) |
| Rate limiting | заголовки `X-RateLimit-*` |
| Трассировка | `traceparent` (W3C Trace Context) |
| Внутренние вызовы | gRPC поверх HTTP/2 + mTLS |

---

## 7.2. REST API Specification (OpenAPI Fragment)

```yaml
openapi: 3.0.3
info:
  title: Dota AI Analyst Core API
  version: 2.0.0
paths:
  /api/v1/matches/upload:
    post:
      summary: Загрузка бинарного файла реплея (.dem) для экспресс-анализа
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
          description: Файл принят и поставлен в очередь на обработку ETL
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
          description: Неверный формат файла или повреждённая структура заголовков
        '401':
          description: Ошибка авторизации токена доступа
```

Полный контракт вынесен в отдельный файл: [openapi/dota-ai-analyst.yaml](../openapi/dota-ai-analyst.yaml).

---

## 7.3. Каталог REST-эндпоинтов

### 7.3.1. Матчи и анализ

| Метод | Путь | Назначение | Auth |
|---|---|---|---|
| POST | `/api/v1/matches/upload` | Загрузка реплея | Bearer |
| GET | `/api/v1/matches/{id}` | Метаданные матча | Bearer |
| GET | `/api/v1/matches/{id}/analysis` | Результат анализа | Bearer |
| GET | `/api/v1/matches/{id}/timeline` | Тайм-серия WP/net worth | Bearer |
| GET | `/api/v1/matches/{id}/heatmap` | Данные тепловой карты | Bearer |
| GET | `/api/v1/jobs/{job_id}` | Статус задания | Bearer |

### 7.3.2. Игроки и профили

| Метод | Путь | Назначение | Auth |
|---|---|---|---|
| GET | `/api/v1/players/{id}/profile` | Профиль игрока | Bearer |
| GET | `/api/v1/players/{id}/metrics` | Агрегированные метрики | Bearer |
| GET | `/api/v1/players/{id}/errors` | История ошибок | Premium |
| GET | `/api/v1/players/{id}/training-plan` | План тренировок | Premium |

### 7.3.3. Драфт, похожесть, мета

| Метод | Путь | Назначение | Auth |
|---|---|---|---|
| POST | `/api/v1/draft/simulate` | Симуляция драфта | Bearer |
| POST | `/api/v1/similarity/search` | Поиск похожих матчей/игроков | Bearer |
| GET | `/api/v1/meta/heroes` | Мета героев (винрейты) | public |
| GET | `/api/v1/meta/trends` | Тренды меты | public |

### 7.3.4. Live (WebSocket)

| Протокол | Путь | Назначение |
|---|---|---|
| WS | `/api/v1/live/{match_id}` | Стрим Win Probability |
| WS | `/api/v1/live/draft/{session_id}` | Live-симуляция драфта |

---

## 7.4. Модели данных ответов (примеры)

### 7.4.1. Результат анализа матча

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
          "explanation": "Смерть в тумане войны без обзора рядом с врагами."
        }
      ]
    }
  ],
  "narrative": "AI Coach: команда доминировала в мидгейме...",
  "partial": false
}
```

### 7.4.2. Запрос симуляции драфта

```json
{
  "patch": "7.36c",
  "radiant_picks": [8, 74],
  "dire_picks": [1, 35],
  "bans": [86, 41],
  "next_action": "radiant_pick"
}
```

Ответ:

```json
{
  "predicted_winrate_radiant": 0.54,
  "recommendations": [
    { "hero_id": 26, "expected_winrate": 0.58, "reason": "синергия + контра к Dire" },
    { "hero_id": 5,  "expected_winrate": 0.56, "reason": "усиление лейнинга" }
  ]
}
```

---

## 7.5. Единый формат ошибок (RFC 7807)

```json
{
  "type": "https://api.dota-ai-analyst.com/errors/invalid-replay",
  "title": "Invalid replay file",
  "status": 400,
  "detail": "Файл не является валидным Source 2 Demo (.dem).",
  "instance": "/api/v1/matches/upload",
  "trace_id": "9f8e7d6c-..."
}
```

### 7.5.1. Таблица кодов ошибок

| HTTP | Код (type) | Значение |
|---|---|---|
| 400 | `invalid-replay` | Некорректный/повреждённый файл |
| 401 | `unauthorized` | Отсутствует/невалиден токен |
| 403 | `forbidden` | Недостаточно прав (RBAC) |
| 404 | `not-found` | Ресурс не найден |
| 409 | `duplicate-job` | Идемпотентный конфликт |
| 422 | `validation-error` | Ошибка валидации полей |
| 429 | `rate-limited` | Превышен лимит запросов |
| 500 | `internal-error` | Внутренняя ошибка |
| 503 | `service-unavailable` | Downstream недоступен |

---

## 7.6. Внутренние контракты gRPC

Полные `.proto` вынесены в [каталог proto](../proto/). Обзор сервисов:

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

### 7.6.1. Сопоставление REST → gRPC

| REST-эндпоинт | Внутренний gRPC-вызов |
|---|---|
| `GET /matches/{id}/analysis` | `MLService.Predict` + `LLM` (async) |
| `POST /draft/simulate` | `DraftService.SimulateDraft` |
| `POST /similarity/search` | `SimilarityService.FindSimilar` |
| `GET /players/{id}/training-plan` | `RecommendationService.BuildPlan` |
| `WS /live/{match_id}` | `MLService.PredictStream` |

---

## 7.7. Версионирование, совместимость и депрекация

| Аспект | Политика |
|---|---|
| Версия API | в пути (`/api/v1`); v2 при breaking changes |
| Совместимость gRPC/Avro | `BACKWARD` (можно добавлять optional-поля) |
| Депрекация | заголовок `Deprecation` + `Sunset`, минимум 6 мес |
| Обнаружение изменений | контрактные тесты в CI (см. Гл. 10) |

---

## 7.8. Ограничения запросов (Rate Limits)

| Тариф | Лимит (запросов/мин) | Загрузок реплеев/день |
|---|---|---|
| Free | 60 | 5 |
| Premium | 300 | 100 |
| Pro (B2B) | 1200 | 2000 |
| Internal | без лимита | без лимита |

Заголовки ответа: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
При превышении — `429` + `Retry-After`.
