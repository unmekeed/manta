"""Онлайн-слой Feature Store поверх Redis (Гл. 3.6, C4 роадмапа).

Схема хранения: один Redis-hash на сущность внутри feature view —

    fs:{view}:{entity}   →   {feature: value, ..., _ts: unix_seconds}

entity — канонизированные ключи сущности («match_id=123» или
«match_id=123|player_id=4», сортировка по имени ключа). Значения — str(float);
NaN хранится как «nan» и честно возвращается обратно.

TTL на каждой записи (FS_TTL_S, по умолчанию 7 дней): онлайн-слой — это
«текущее состояние», исторические фичи живут в ClickHouse-витринах.
"""
from __future__ import annotations

import math
import time

# Поля-значения, по которым распознаётся сущность вектора при записи:
# WriteFeatures в proto не несёт entity_keys, поэтому ключи сущности
# передаются внутри values (match_id обязателен, player_id опционален).
ENTITY_FIELDS = ("match_id", "player_id")


def entity_of(values: dict[str, float]) -> str | None:
    """Канонический идентификатор сущности из значений вектора."""
    parts = [f"{k}={int(values[k])}" for k in ENTITY_FIELDS
             if k in values and not math.isnan(values[k])]
    return "|".join(parts) if parts else None


class RedisFeatureStore:
    def __init__(self, client, ttl_s: int = 7 * 24 * 3600):
        self._r = client
        self._ttl = ttl_s

    @staticmethod
    def _key(view: str, entity: str) -> str:
        return f"fs:{view}:{entity}"

    def write(self, view: str, vectors: list[dict[str, float]],
              ts: float | None = None) -> int:
        """Записать векторы; вернуть число принятых (без сущности — пропуск)."""
        written = 0
        stamp = ts if ts is not None else time.time()
        for values in vectors:
            entity = entity_of(values)
            if entity is None:
                continue
            key = self._key(view, entity)
            payload = {k: repr(float(v)) for k, v in values.items()}
            payload["_ts"] = repr(float(stamp))
            self._r.hset(key, mapping=payload)
            if self._ttl:
                self._r.expire(key, self._ttl)
            written += 1
        return written

    def read(self, refs: list[str],
             entity_keys: dict[str, str]) -> tuple[dict[str, float], float]:
        """Значения по ссылкам «view:feature» для одной сущности.

        Возвращает ({ref: value}, event_ts). Отсутствующие фичи опускаются —
        вызывающая сторона решает, что делать с неполным вектором.
        """
        entity = "|".join(f"{k}={entity_keys[k]}"
                          for k in sorted(entity_keys))
        by_view: dict[str, list[str]] = {}
        for ref in refs:
            view, _, feature = ref.partition(":")
            if feature:
                by_view.setdefault(view, []).append(feature)

        out: dict[str, float] = {}
        ts = 0.0
        for view, features in by_view.items():
            raw = self._r.hgetall(self._key(view, entity))
            if not raw:
                continue
            decoded = {(k.decode() if isinstance(k, bytes) else k):
                       (v.decode() if isinstance(v, bytes) else v)
                       for k, v in raw.items()}
            ts = max(ts, float(decoded.get("_ts", 0.0)))
            for f in features:
                if f in decoded:
                    out[f"{view}:{f}"] = float(decoded[f])
        return out, ts
