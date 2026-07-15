"""Тесты чистого билдера отчёта."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reportgen.builder import (build_analysis, build_narrative,
                               build_timeline, _turning_point)

ROWS = [
    {"game_time": 60, "networth_diff": 0},
    {"game_time": 120, "networth_diff": 500},
    {"game_time": 180, "networth_diff": -3000},
]
WP = [0.5, 0.55, 0.2]

PLAYERS = [
    {"player_id": 0, "hero": "npc_dota_hero_axe", "player_name": "A",
     "gpm": 500.0, "lh_at_10": 60, "dn_at_10": 10, "gold_share": 0.3},
    {"player_id": 5, "hero": "npc_dota_hero_kez", "player_name": "B",
     "gpm": 300.0, "lh_at_10": 8, "dn_at_10": 0, "gold_share": 0.15},
]


def test_timeline_schema():
    t = build_timeline(1, ROWS, WP)
    assert t["match_id"] == 1
    assert t["points"][2] == {"game_time": 180, "radiant_wp": 0.2,
                              "net_worth_diff": -3000}


def test_turning_point_finds_biggest_swing():
    t = build_timeline(1, ROWS, WP)
    tp = _turning_point(t["points"])
    # Дельта считается по сглаженной (median-3) кривой:
    # [0.5, 0.55, 0.2] → [0.5, 0.5, 0.2], перелом −0.3 на 180-й секунде.
    assert tp == {"game_time": 180, "delta_wp": -0.3}


def test_turning_point_ignores_single_spike():
    """Одноточечный выброс калибровки — не переломный момент."""
    pts = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
           for t, w in [(60, 0.5), (120, 0.5), (180, 0.9), (240, 0.5),
                        (300, 0.5)]]
    assert _turning_point(pts) is None


def test_turning_point_ignores_flat_game():
    pts = [{"game_time": t, "radiant_wp": 0.5, "net_worth_diff": 0}
           for t in (60, 120, 180)]
    assert _turning_point(pts) is None


def test_analysis_schema_and_scores():
    t = build_timeline(1, ROWS, WP)
    a = build_analysis(1, "Dire", PLAYERS, t, "0.1.0-x")
    assert a["win_probability"]["final_radiant"] == 0.2
    assert a["partial"] is True and a["model_version"] == "0.1.0-x"
    p0 = a["players"][0]
    assert p0["player_id"] == 0
    assert 0.0 <= p0["laning_score"] <= 1.0
    assert p0["laning_score"] > a["players"][1]["laning_score"]
    assert p0["impact_score"] > a["players"][1]["impact_score"]
    assert p0["errors"] == []


def test_narrative_mentions_winner_turning_and_top_farm():
    t = build_timeline(1, ROWS, WP)
    a = build_analysis(1, "Dire", PLAYERS, t, "v")
    n = a["narrative"]
    assert "Dire" in n
    assert "3-й минуте" in n          # переломный момент на 180 c
    assert "axe" in n and "500 GPM" in n


def test_narrative_without_turning_point():
    n = build_narrative("Radiant", PLAYERS, None)
    assert "Radiant" in n and "Переломный" not in n
