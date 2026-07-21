"""Тесты шардирования сбора между машинами (класс Shard + источники)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources import Shard  # noqa: E402
from collector.sources.opendota_timeline import OpenDotaTimelineSource  # noqa: E402


def test_single_machine_default_accepts_all():
    s = Shard()  # count=1
    assert all(s.accepts(m) for m in range(1000))


def test_two_shards_partition_disjointly_and_cover_all():
    a, b = Shard(0, 2), Shard(1, 2)
    ids = list(range(10_000, 20_000))
    sa = {m for m in ids if a.accepts(m)}
    sb = {m for m in ids if b.accepts(m)}
    assert sa.isdisjoint(sb)          # ни один матч не тянут обе машины
    assert sa | sb == set(ids)        # вместе покрывают весь поток
    # деление примерно поровну (match_id плотны)
    assert abs(len(sa) - len(sb)) <= 1


def test_three_shards_balanced():
    shards = [Shard(i, 3) for i in range(3)]
    ids = list(range(30_000, 39_000))
    sizes = [sum(sh.accepts(m) for m in ids) for sh in shards]
    assert sum(sizes) == len(ids)
    assert max(sizes) - min(sizes) <= 1


def test_bad_shard_rejected():
    with pytest.raises(ValueError):
        Shard(2, 2)                   # shard_id вне [0, count)
    with pytest.raises(ValueError):
        Shard(0, 0)                   # count < 1
    with pytest.raises(ValueError):
        Shard(-1, 3)


def test_timeline_source_skips_foreign_shard_before_detail_call(monkeypatch):
    """Матч чужого шарда отсекается ДО дорогого /matches/{id} — квота
    второй машины на него не тратится."""
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0, shard=Shard(0, 2))
    detail_calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def _match(mid):
        return {"match_id": mid, "radiant_win": True, "duration": 1200,
                "lobby_type": 7, "game_mode": 22, "patch": 60,
                "radiant_gold_adv": [i * 100 for i in range(21)],
                "radiant_xp_adv": [i * 120 for i in range(21)],
                "players": [{"player_slot": s, "rank_tier": 80,
                             "kills_log": []} for s in (0, 1, 128, 129, 130)]}

    def fake_get(path, **params):
        if path == "parsedMatches":
            # чётные и нечётные вперемешку
            return FakeResp([{"match_id": m} for m in (11, 10, 9, 8, 7, 6)])
        mid = int(path.split("/")[1])
        detail_calls.append(mid)
        return FakeResp(_match(mid))

    monkeypatch.setattr(src, "_get", fake_get)
    got = [t.match_id for t in src.fetch_new()]
    # shard 0 из 2 → только чётные match_id
    assert all(m % 2 == 0 for m in got)
    assert all(m % 2 == 0 for m in detail_calls)  # нечётные не запрашивались
    assert 11 not in detail_calls and 9 not in detail_calls
