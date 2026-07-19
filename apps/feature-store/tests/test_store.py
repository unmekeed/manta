"""Тесты онлайн-слоя Feature Store на in-memory фейке Redis."""
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from store.redis_store import RedisFeatureStore, entity_of


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def expire(self, key, ttl):
        self.ttls[key] = ttl

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def test_entity_of_match_and_player():
    assert entity_of({"match_id": 42.0, "networth_diff": 100.0}) == "match_id=42"
    assert entity_of({"match_id": 42.0, "player_id": 3.0}) == "match_id=42|player_id=3"
    assert entity_of({"networth_diff": 1.0}) is None
    assert entity_of({"match_id": float("nan")}) is None


def test_write_read_roundtrip():
    r = FakeRedis()
    st = RedisFeatureStore(r, ttl_s=60)
    n = st.write("match_timeline",
                 [{"match_id": 42, "networth_diff": 1500.0, "xp_diff": -200.0}],
                 ts=1000.0)
    assert n == 1
    assert r.ttls["fs:match_timeline:match_id=42"] == 60

    values, ts = st.read(
        ["match_timeline:networth_diff", "match_timeline:xp_diff"],
        {"match_id": "42"})
    assert values == {"match_timeline:networth_diff": 1500.0,
                      "match_timeline:xp_diff": -200.0}
    assert ts == 1000.0


def test_read_missing_features_omitted_and_nan_preserved():
    r = FakeRedis()
    st = RedisFeatureStore(r)
    st.write("v", [{"match_id": 7, "position_advance": float("nan")}], ts=5.0)
    values, _ = st.read(["v:position_advance", "v:no_such"], {"match_id": "7"})
    assert set(values) == {"v:position_advance"}
    assert math.isnan(values["v:position_advance"])


def test_read_unknown_entity_empty():
    st = RedisFeatureStore(FakeRedis())
    values, ts = st.read(["v:x"], {"match_id": "404"})
    assert values == {} and ts == 0.0


def test_vector_without_entity_skipped():
    st = RedisFeatureStore(FakeRedis())
    assert st.write("v", [{"networth_diff": 1.0}]) == 0


def test_write_default_ts_is_now():
    r = FakeRedis()
    st = RedisFeatureStore(r)
    st.write("v", [{"match_id": 1, "a": 2.0}])
    _, ts = st.read(["v:a"], {"match_id": "1"})
    assert abs(ts - time.time()) < 5
