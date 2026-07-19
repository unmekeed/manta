"""Тесты JSON-таймлайн источника (opendota_timeline) и его раннера."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources.opendota_timeline import (OpenDotaTimelineSource,
                                                 match_passes, timeline_rows)


def _parsed_match(mid=100, minutes=20, radiant_win=True):
    """Минимальный JSON распаршенного матча OpenDota."""
    return {
        "match_id": mid,
        "radiant_win": radiant_win,
        "duration": minutes * 60,
        "lobby_type": 7,
        "game_mode": 22,
        "patch": 60,
        "radiant_gold_adv": [i * 100 for i in range(minutes + 1)],
        "radiant_xp_adv": [i * 120 for i in range(minutes + 1)],
        "players": [
            # Radiant (slot < 128): убийства на 90с и 400с
            {"player_slot": 0, "rank_tier": 80,
             "kills_log": [{"time": 90}, {"time": 400}]},
            {"player_slot": 1, "rank_tier": 81, "kills_log": []},
            # Dire: убийство на 200с
            {"player_slot": 128, "rank_tier": 80,
             "kills_log": [{"time": 200}]},
            {"player_slot": 129, "rank_tier": 79, "kills_log": []},
            {"player_slot": 130, "rank_tier": 80, "kills_log": []},
        ],
    }


def test_timeline_rows_grid_and_kills():
    m = _parsed_match(minutes=20)
    # gold_t игроков → networth_total по минутам (сумма обеих команд)
    for p in m["players"]:
        p["gold_t"] = [i * 200 for i in range(21)]
    rows = timeline_rows(m)
    # сетка минут: 60..1200 (нулевая пропущена)
    assert [r["game_time"] for r in rows] == [i * 60 for i in range(1, 21)]
    assert rows[0]["networth_diff"] == 100 and rows[-1]["networth_diff"] == 2000
    # 5 игроков в фикстуре × 200·i золота
    assert rows[0]["networth_total"] == 5 * 200.0
    assert rows[-1]["networth_total"] == 5 * 200.0 * 20
    # убийства накопительно: к 60с — 0; к 120с — 1 (Radiant, 90с);
    # к 240с — 1R + 1D (200с); к 420с — 2R
    by_t = {r["game_time"]: r for r in rows}
    assert (by_t[60]["kills_radiant"], by_t[60]["kills_dire"]) == (0, 0)
    assert (by_t[120]["kills_radiant"], by_t[120]["kills_dire"]) == (1, 0)
    assert (by_t[240]["kills_radiant"], by_t[240]["kills_dire"]) == (1, 1)
    assert by_t[420]["kills_radiant"] == 2
    # позиций в JSON нет → NaN (не 0!)
    assert all(math.isnan(r["position_advance"]) for r in rows)
    assert all(r["radiant_win"] == 1 for r in rows)


def test_match_passes_filters():
    ok, _ = match_passes(_parsed_match(), 80, 900, 60)
    assert ok
    turbo = _parsed_match(); turbo["game_mode"] = 23
    assert match_passes(turbo, 80, 900, 60) == (False, "mode")
    short = _parsed_match(minutes=5)
    assert match_passes(short, 80, 900, 60) == (False, "short")
    low = _parsed_match()
    for p in low["players"]:
        p["rank_tier"] = 50
    assert match_passes(low, 80, 900, 60) == (False, "low-rank")
    old = _parsed_match(); old["patch"] = 55
    assert match_passes(old, 80, 900, 60) == (False, "old-patch")
    unparsed = _parsed_match(); unparsed["radiant_gold_adv"] = None
    assert match_passes(unparsed, 80, 900, 60) == (False, "no-timeline")


def test_fetch_new_skips_collected_before_detail_call(monkeypatch):
    """Дедуп срабатывает ДО дорогого вызова /matches/{id} — бюджет API
    не тратится на уже собранные матчи."""
    src = OpenDotaTimelineSource(limit_per_cycle=2, min_patch=60,
                                 api_delay_s=0)
    detail_calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_get(path, **params):
        if path == "parsedMatches":
            return FakeResp([{"match_id": m} for m in (5, 4, 3, 2, 1)])
        assert path.startswith("matches/")
        mid = int(path.split("/")[1])
        detail_calls.append(mid)
        return FakeResp(_parsed_match(mid=mid))

    monkeypatch.setattr(src, "_get", fake_get)
    got = list(src.fetch_new(skip=lambda mid: mid in {5, 3}))
    assert [t.match_id for t in got] == [4, 2]        # 5 и 3 пропущены
    assert detail_calls == [4, 2]                     # без лишних вызовов
    assert got[0].tier == "Premium"
    assert len(got[0].rows) == 20


def test_runner_inserts_and_marks(monkeypatch):
    """Раннер: вставка строк в CH (nan как текст) + отметка в PG."""
    from collector import timeline_runner
    from collector.sources.opendota_timeline import TimelineMatch

    inserted = {}

    class FakeCur:
        def __init__(self, store): self._s = store
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, params=None):
            self._s.setdefault("sql", []).append((q.split()[0], params))
            self._q = q
        def fetchone(self):
            return None  # ничего не собрано

    class FakeDB:
        def __init__(self, store): self._s = store
        def cursor(self): return FakeCur(self._s)
        def close(self): pass

    pg_store = {}
    monkeypatch.setattr(timeline_runner.psycopg, "connect",
                        lambda dsn, autocommit: FakeDB(pg_store))

    def fake_post(url, params=None, data=None, headers=None, timeout=None):
        inserted["query"] = params["query"]
        inserted["body"] = data.decode()
        class R:
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr(timeline_runner.requests, "post", fake_post)

    class OneShotSource:
        name = "opendota_timeline"
        def fetch_new(self, skip=None):
            rows = timeline_rows(_parsed_match(mid=42, minutes=3))
            yield TimelineMatch(match_id=42, tier="Premium", rows=rows,
                                source_cursor="42")

    coll = timeline_runner.TimelineCollector(
        timeline_runner.TimelineConfig(), OneShotSource())
    assert coll.collect_once() == 1
    assert "MatchTimelineFeatures" in inserted["query"]
    lines = inserted["body"].strip().split("\n")
    assert len(lines) == 3
    first = lines[0].split("\t")
    assert first[0] == "42" and first[1] == "60"
    # position_advance + alive_diff + networth_total (фикстура без gold_t)
    assert lines[0].count("nan") == 3
    assert "opendota-json@3" in lines[0]
    # PG: INSERT в CollectedMatches и CollectorCursor
    kinds = [k for k, _ in pg_store["sql"]]
    assert kinds.count("INSERT") == 2


def test_timeline_rows_building_diffs_from_objectives():
    """towers_diff/rax_diff из objectives: снесённое goodguys-здание — очко
    Dire (−1), badguys — очко Radiant (+1), накопительно по минутам."""
    m = _parsed_match(minutes=10)
    m["objectives"] = [
        {"type": "building_kill", "time": 130,
         "key": "npc_dota_badguys_tower1_mid"},     # Radiant снёс: +1
        {"type": "building_kill", "time": 250,
         "key": "npc_dota_goodguys_tower1_top"},    # Dire снёс: -1
        {"type": "building_kill", "time": 400,
         "key": "npc_dota_badguys_melee_rax_bot"},  # ракс Radiant'ом: +1
        {"type": "CHAT_MESSAGE_FIRSTBLOOD", "time": 90},  # не здание
    ]
    rows = timeline_rows(m)
    by_t = {r["game_time"]: r for r in rows}
    assert by_t[120]["towers_diff"] == 0.0
    assert by_t[180]["towers_diff"] == 1.0
    assert by_t[300]["towers_diff"] == 0.0     # +1 и −1
    assert by_t[360]["rax_diff"] == 0.0
    assert by_t[420]["rax_diff"] == 1.0
    # alive недоступен из JSON
    import math as _m
    assert all(_m.isnan(r["alive_diff"]) for r in rows)


def test_pro_mode_filters_and_tier(monkeypatch):
    """pro-режим: кандидаты из /proMatches, tier=Professional, фильтр без
    рангов/лобби (CM в турнирном лобби проходит), короткие отсекаются."""
    src = OpenDotaTimelineSource(limit_per_cycle=2, min_patch=60,
                                 api_delay_s=0, mode="pro")
    assert src.name == "opendota_timeline_pro"

    def _pro_match(mid, minutes=25):
        m = _parsed_match(mid=mid, minutes=minutes)
        m["lobby_type"] = 1      # турнирное лобби
        m["game_mode"] = 2       # Captains Mode
        for p in m["players"]:
            p.pop("rank_tier", None)   # у про-игроков ранги скрыты
        return m

    calls = []

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    def fake_get(path, **params):
        calls.append(path)
        if path == "proMatches":
            return FakeResp([{"match_id": m} for m in (9, 8, 7)])
        mid = int(path.split("/")[1])
        return FakeResp(_pro_match(mid, minutes=25 if mid != 8 else 5))

    monkeypatch.setattr(src, "_get", fake_get)
    got = list(src.fetch_new(skip=lambda mid: False))
    assert "proMatches" in calls
    assert [t.match_id for t in got] == [9, 7]     # 8 отсечён (5 минут)
    assert all(t.tier == "Professional" for t in got)


def test_pro_match_passes_ignores_rank_and_lobby():
    m = _parsed_match()
    m["lobby_type"] = 1
    m["game_mode"] = 2
    for p in m["players"]:
        p.pop("rank_tier", None)
    # public-фильтр отверг бы (lobby), pro — пропускает
    assert match_passes(m, 80, 900, 60)[0] is False
    assert match_passes(m, 80, 900, 60, pro=True) == (True, "ok")
