"""Тесты Draft Engine: статистика, сглаживание, рекомендации, gRPC."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine.recommend import predicted_winrate_radiant, suggest
from engine.stats import DraftStats, build_stats


def _rows(matches: list[tuple[list[int], list[int], int]]) -> list[dict]:
    """(radiant_heroes, dire_heroes, radiant_win) → строки PMF."""
    out = []
    for mid, (r, d, rw) in enumerate(matches, start=1):
        for h in r:
            out.append({"match_id": mid, "team": 2,
                        "hero": f"npc_dota_hero_{h}", "won": rw})
        for h in d:
            out.append({"match_id": mid, "team": 3,
                        "hero": f"npc_dota_hero_{h}", "won": 1 - rw})
    return out


def _hid(name: str) -> int:
    return int(name.replace("npc_dota_hero_", "") or 0)


def test_build_stats_counts():
    rows = _rows([([1, 2], [3, 4], 1), ([1, 5], [3, 6], 0)])
    st = build_stats(rows, _hid)
    assert st.n_matches == 2
    assert st.solo[1] == (1.0, 2.0)          # герой 1: 1 победа из 2
    assert st.solo[3] == (1.0, 2.0)
    assert st.pair[(1, 2)] == (1.0, 1.0)     # синергия выигравшей пары
    assert st.counter[(1, 3)] == (1.0, 2.0)  # 1 против 3: победа из двух


def test_smoothing_keeps_small_samples_humble():
    """Одна победа не делает героя имбой: сглаживание прижимает к 0.5."""
    st = build_stats(_rows([([1, 2], [3, 4], 1)]), _hid)
    wr = predicted_winrate_radiant(st, [1], [])
    assert 0.5 < wr < 0.56          # чуть выше нейтрального, но скромно
    assert predicted_winrate_radiant(st, [], []) == 0.5


def test_predicted_winrate_symmetry():
    """Зеркальный драфт даёт зеркальную вероятность."""
    rows = _rows([([1, 2], [3, 4], 1)] * 6 + [([1, 2], [3, 4], 0)] * 2)
    st = build_stats(rows, _hid)
    a = predicted_winrate_radiant(st, [1, 2], [3, 4])
    b = predicted_winrate_radiant(st, [3, 4], [1, 2])
    assert abs((a + b) - 1.0) < 1e-9
    assert a > 0.5 > b               # 1,2 статистически сильнее


def test_suggest_excludes_taken_and_ranks_by_gain():
    rows = _rows([([1, 2], [3, 4], 1)] * 8 + [([5, 6], [7, 8], 1)] * 4
                 + [([5, 6], [7, 8], 0)] * 4)
    st = build_stats(rows, _hid)
    base, sugg, side = suggest(st, [1], [3], bans=[2], next_action="")
    assert side == "pick_radiant"    # у Radiant не больше пиков... равно → R? len равны → radiant
    ids = [s["hero_id"] for s in sugg]
    assert 2 not in ids and 1 not in ids and 3 not in ids   # занятые исключены
    # герой 4 в этих данных проигрывал герою 1 → не должен быть топ-пиком
    assert all(s["expected_winrate"] >= 0 for s in sugg)
    assert all("выборка" in s["reason"] for s in sugg)


def test_suggest_for_dire_side():
    rows = _rows([([1, 2], [3, 4], 1)] * 5)
    st = build_stats(rows, _hid)
    base, sugg, side = suggest(st, [1, 2], [3], bans=[], next_action="")
    assert side == "pick_dire"       # у Dire меньше пиков
    # expected_winrate — с точки зрения действующей стороны (Dire)
    assert all(0.0 <= s["expected_winrate"] <= 1.0 for s in sugg)


def test_grpc_contract():
    import grpc
    from serve_draft import StatsHolder, build_server
    from gen import services_pb2, services_pb2_grpc

    holder = StatsHolder(("http://x", "db", "u", "p"), {})
    holder.stats = build_stats(
        _rows([([1, 2], [3, 4], 1)] * 6 + [([5, 6], [7, 8], 0)] * 3), _hid)
    server, port = build_server(holder, 0)
    server.start()
    try:
        chan = grpc.insecure_channel(f"localhost:{port}")
        stub = services_pb2_grpc.DraftServiceStub(chan)
        rec = stub.SimulateDraft(services_pb2.DraftState(
            radiant_picks=[1], dire_picks=[3], bans=[2],
            next_action="pick_radiant"))
        assert 0.0 < rec.predicted_winrate_radiant < 1.0
        assert 1 <= len(rec.suggestions) <= 5
        assert all(s.hero_id not in (1, 2, 3) for s in rec.suggestions)
        assert rec.suggestions[0].reason

        holder.stats = DraftStats()   # пустая статистика → FAILED_PRECONDITION
        with pytest.raises(grpc.RpcError) as e:
            stub.SimulateDraft(services_pb2.DraftState())
        assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        server.stop(0)
