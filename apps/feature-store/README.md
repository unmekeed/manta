# Feature Store — онлайн-слой (Гл. 3.6, C4)

Низколатентные «текущие» фичи поверх Redis для live-инференса. Истина —
ClickHouse-витрины; онлайн-слой — кэш последнего состояния с TTL.

Контракт — `proto/services.proto` → `FeatureStore`:

- `WriteFeatures(FeatureBatch)` — векторы кладутся в view батча; сущность
  вектора — `match_id` (+`player_id`) внутри `values` (proto-версия
  `FeatureVector` не несёт entity-ключей отдельно);
- `GetOnlineFeatures(FeatureRequest)` — `feature_refs` вида
  `view:feature`, `entity_keys` — идентификатор сущности; сущность вне
  слоя или истёкший TTL → `NOT_FOUND`.

Схема Redis: `fs:{view}:{entity}` → hash `{feature: value, _ts: unix}`;
TTL `FS_TTL_S` (7 дней). NaN сохраняется и возвращается честно.

Писатель — feature-extractor: после расчёта фич матча пушит последний
timeline-срез в view `match_timeline` (env `FEATURE_STORE_ADDR`, пусто —
выключено; сбой стора не роняет обработку — это кэш).

## Запуск

```bash
make fs-serve          # gRPC :50055, метрики :9114
pytest tests/
```
