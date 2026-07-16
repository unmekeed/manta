"""Тесты чистого билдера отчёта."""
import sys
from pathlib import Path

import pytest

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
    # Без событий атрибуции импакт нейтрален (0.5) у обоих.
    assert p0["impact_score"] == a["players"][1]["impact_score"] == 0.5
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


def test_detect_errors_attributes_team_drop_to_deaths():
    from reportgen.builder import detect_errors

    pts = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
           for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                        (300, 0.3)]]
    hero_player = {"npc_dota_hero_axe": 0, "npc_dota_hero_puck": 2,
                   "npc_dota_hero_kez": 5}
    player_team = {0: 2, 2: 2, 5: 3}
    kills = [
        {"game_time": 200, "target": "npc_dota_hero_axe"},   # Radiant умер
        {"game_time": 230, "target": "npc_dota_hero_puck"},  # Radiant умер
        {"game_time": 235, "target": "npc_dota_hero_kez"},   # Dire (не в счёт)
        {"game_time": 500, "target": "npc_dota_hero_axe"},   # вне окна падения
    ]
    errors = detect_errors(pts, kills, hero_player, player_team)
    # Падение 0.2 в окне 180-240 делится между двумя смертями Radiant.
    assert set(errors) == {0, 2}
    e0 = errors[0][0]
    assert e0["type"] == "critical_death" and e0["game_time"] == 200
    assert e0["delta_wp"] == -0.1
    assert "axe" in e0["explanation"]
    assert 5 not in errors            # смерть Dire в окне падения Radiant
    assert len(errors[0]) == 1        # смерть на 500с не в критическом окне


def test_detect_errors_quiet_game():
    from reportgen.builder import detect_errors

    pts = [{"game_time": t, "radiant_wp": 0.5, "net_worth_diff": 0}
           for t in range(60, 601, 60)]
    kills = [{"game_time": 300, "target": "npc_dota_hero_axe"}]
    assert detect_errors(pts, kills, {"npc_dota_hero_axe": 0}, {0: 2}) == {}


def test_analysis_includes_errors():
    t = build_timeline(1, ROWS, WP)  # падение 0.5→0.2 в окне 120-180
    players = [dict(p, team=(2 if p["player_id"] == 0 else 3))
               for p in PLAYERS]
    kills = [{"game_time": 150, "target": "npc_dota_hero_axe"}]
    a = build_analysis(1, "Dire", players, t, "v", kills=kills)
    p0 = next(p for p in a["players"] if p["player_id"] == 0)
    assert len(p0["errors"]) == 1
    assert p0["errors"][0]["type"] == "critical_death"
    assert p0["errors"][0]["delta_wp"] < 0



def test_wp_attribution_credits_and_debits():
    from reportgen.builder import wp_attribution

    pts = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
           for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                        (300, 0.3)]]
    hero_player = {"npc_dota_hero_axe": 0, "npc_dota_hero_kez": 5,
                   "npc_dota_hero_slardar": 6}
    player_team = {0: 2, 5: 3, 6: 3}
    kills = [
        # Окно 180-240 (WP Radiant -0.2): kez убил axe.
        {"game_time": 200, "target": "npc_dota_hero_axe",
         "attacker": "npc_dota_hero_kez"},
        {"game_time": 210, "target": "npc_dota_hero_axe",
         "attacker": "npc_dota_hero_slardar"},
    ]
    errors, impact = wp_attribution(pts, kills, hero_player, player_team)
    # Дебет: обе смерти axe в окне делят -0.2.
    assert impact[0] == pytest.approx(-0.2)
    assert len(errors[0]) == 2
    # Кредит: kez и slardar делят +0.2 поровну.
    assert impact[5] == pytest.approx(0.1)
    assert impact[6] == pytest.approx(0.1)


def test_impact_in_analysis_orders_players():
    t = build_timeline(1, ROWS, WP)  # падение Radiant в окне 120-180
    players = [dict(p, team=(2 if p["player_id"] == 0 else 3))
               for p in PLAYERS]
    kills = [{"game_time": 150, "target": "npc_dota_hero_axe",
              "attacker": "npc_dota_hero_kez"}]
    a = build_analysis(1, "Dire", players, t, "v", kills=kills)
    by_pid = {p["player_id"]: p for p in a["players"]}
    assert by_pid[5]["impact_score"] > 0.5 > by_pid[0]["impact_score"]
    assert by_pid[5]["delta_wp_sum"] == -by_pid[0]["delta_wp_sum"]


def test_safety_index_geometry():
    from reportgen.builder import index_positions, safety_index

    def track(hero, x, y):
        return [{"game_time": t, "hero": hero, "x": x, "y": y}
                for t in (100, 110, 120)]

    hero_team = {"axe": 2, "kez": 3, "slardar": 3}
    # Аксе глубоко на половине Dire, два врага вплотную → высокий риск.
    deep = index_positions(
        track("CDOTA_Unit_Hero_Axe", 6000, 6000)
        + track("CDOTA_Unit_Hero_Kez", 6200, 6100)
        + track("CDOTA_Unit_Hero_Slardar", 5800, 6200))
    si_deep = safety_index("npc_dota_hero_axe", 2, 120, deep, hero_team)
    # Аксе у своей базы, враги на другом конце карты → низкий риск.
    safe = index_positions(
        track("CDOTA_Unit_Hero_Axe", -7000, -6800)
        + track("CDOTA_Unit_Hero_Kez", 7000, 6800)
        + track("CDOTA_Unit_Hero_Slardar", 6500, 7000))
    si_safe = safety_index("npc_dota_hero_axe", 2, 120, safe, hero_team)
    assert si_deep > 0.75
    assert si_safe < 0.05
    assert si_deep > si_safe


def test_safety_index_ignores_stale_and_unknown():
    from reportgen.builder import index_positions, safety_index

    pts = index_positions(
        [{"game_time": 10, "hero": "CDOTA_Unit_Hero_Axe", "x": 0, "y": 0}])
    # Снапшот старше SI_STALE_S → позиции жертвы нет → SI 0.
    assert safety_index("npc_dota_hero_axe", 2, 500, pts, {"axe": 2}) == 0.0
    # Нет данных о герое вообще.
    assert safety_index("npc_dota_hero_kez", 3, 10, pts, {"axe": 2}) == 0.0


def test_errors_carry_safety_index():
    from reportgen.builder import index_positions, wp_attribution

    pts_wp = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
              for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                           (300, 0.3)]]
    positions = []
    for t in range(180, 241, 10):
        positions.append({"game_time": t, "hero": "CDOTA_Unit_Hero_Axe",
                          "x": 6000, "y": 6000})
        positions.append({"game_time": t, "hero": "CDOTA_Unit_Hero_Kez",
                          "x": 6100, "y": 6050})
        positions.append({"game_time": t, "hero": "CDOTA_Unit_Hero_Slardar",
                          "x": 5900, "y": 6100})
    kills = [{"game_time": 200, "target": "npc_dota_hero_axe",
              "attacker": "npc_dota_hero_kez"}]
    errors, _ = wp_attribution(
        pts_wp, kills,
        {"npc_dota_hero_axe": 0, "npc_dota_hero_kez": 5,
         "npc_dota_hero_slardar": 6},
        {0: 2, 5: 3, 6: 3}, positions_by_hero=index_positions(positions))
    e = errors[0][0]
    assert e["safety_index"] >= 0.6  # два врага вплотную, глубокий заход
    assert "риск" in e["explanation"].lower()


def test_heatmap_grid_and_teams():
    from reportgen.builder import HEATMAP_GRID, MAP_BOUND, build_heatmap

    players = [
        {"player_id": 0, "hero": "npc_dota_hero_axe", "player_name": "A",
         "team": 2},
        {"player_id": 5, "hero": "npc_dota_hero_kez", "player_name": "B",
         "team": 3},
    ]
    positions = [
        # Axe у базы Radiant (юго-запад) — дважды в одной ячейке.
        {"game_time": 10, "hero": "CDOTA_Unit_Hero_Axe",
         "x": -MAP_BOUND, "y": -MAP_BOUND},
        {"game_time": 11, "hero": "CDOTA_Unit_Hero_Axe",
         "x": -MAP_BOUND, "y": -MAP_BOUND},
        # Kez у базы Dire (северо-восток), координата за границей — клип.
        {"game_time": 10, "hero": "CDOTA_Unit_Hero_Kez",
         "x": MAP_BOUND + 999, "y": MAP_BOUND},
        # Неизвестный герой игнорируется.
        {"game_time": 10, "hero": "CDOTA_Unit_Hero_Lina", "x": 0, "y": 0},
    ]
    hm = build_heatmap(positions, players)
    assert hm["grid"] == HEATMAP_GRID
    assert len(hm["players"]) == 2

    axe = hm["players"][0]
    assert axe["player_id"] == 0 and axe["team"] == 2
    assert axe["cells"] == [[0, 0, 2]]

    kez = hm["players"][1]
    assert kez["cells"] == [[HEATMAP_GRID - 1, HEATMAP_GRID - 1, 1]]
