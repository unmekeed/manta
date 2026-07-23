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


def test_error_carries_shap_drivers():
    """SHAP-вклады кадра из timeline-точки прикладываются к ошибке."""
    from reportgen.builder import detect_errors

    drv = [{"feature": "networth_diff", "value": -0.8},
           {"feature": "kills_diff", "value": -0.3}]
    pts = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
           for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                        (300, 0.3)]]
    pts[3]["drivers"] = drv  # снапшот конца окна падения
    errors = detect_errors(
        pts, [{"game_time": 200, "target": "npc_dota_hero_axe"}],
        {"npc_dota_hero_axe": 0}, {0: 2})
    err = errors[0][0]
    assert err["top_contributions"] == drv


def test_timeline_points_carry_drivers():
    from reportgen.builder import build_timeline

    rows = [{"game_time": 60, "networth_diff": 100},
            {"game_time": 120, "networth_diff": 300}]
    drivers = [[{"feature": "game_time", "value": 0.1}],
               [{"feature": "networth_diff", "value": 0.2}]]
    tl = build_timeline(1, rows, [0.5, 0.6], drivers)
    assert tl["points"][0]["drivers"] == drivers[0]
    assert tl["points"][1]["drivers"] == drivers[1]
    # без drivers ключ отсутствует (обратная совместимость отчёта)
    tl2 = build_timeline(1, rows, [0.5, 0.6])
    assert "drivers" not in tl2["points"][0]


def test_errors_carry_death_position():
    """Точка смерти нормализуется в доли карты 0..1 для мини-карты (C6)."""
    from reportgen.builder import MAP_BOUND, index_positions, wp_attribution

    pts_wp = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
              for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                           (300, 0.3)]]
    positions = [{"game_time": 200, "hero": "CDOTA_Unit_Hero_Axe",
                  "x": 4600, "y": -4600}]
    kills = [{"game_time": 200, "target": "npc_dota_hero_axe",
              "attacker": "npc_dota_hero_kez"}]
    errors, _ = wp_attribution(
        pts_wp, kills, {"npc_dota_hero_axe": 0, "npc_dota_hero_kez": 5},
        {0: 2, 5: 3}, positions_by_hero=index_positions(positions))
    pos = errors[0][0]["pos"]
    assert pos == {"x": round((4600 + MAP_BOUND) / (2 * MAP_BOUND), 4),
                   "y": round((-4600 + MAP_BOUND) / (2 * MAP_BOUND), 4)}


def test_errors_without_positions_have_no_pos():
    from reportgen.builder import detect_errors

    pts = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
           for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                        (300, 0.3)]]
    errors = detect_errors(
        pts, [{"game_time": 200, "target": "npc_dota_hero_axe"}],
        {"npc_dota_hero_axe": 0}, {0: 2})
    assert "pos" not in errors[0][0]


def test_risk_features_mirror_trainer():
    """Фичи Death-Risk: подсчёт соседей, глубина, мёртвые не в счёт."""
    from reportgen.builder import RISK_FAR, index_positions, risk_features

    positions = [
        {"game_time": 200, "hero": "CDOTA_Unit_Hero_Axe",
         "x": 4000, "y": 4000, "is_alive": 1},          # жертва (Radiant)
        {"game_time": 200, "hero": "CDOTA_Unit_Hero_Kez",
         "x": 4300, "y": 4400, "is_alive": 1},          # враг в 500
        {"game_time": 200, "hero": "CDOTA_Unit_Hero_Slardar",
         "x": 6000, "y": 6000, "is_alive": 0},          # мёртвый враг — мимо
        {"game_time": 200, "hero": "CDOTA_Unit_Hero_Lion",
         "x": 3000, "y": 3500, "is_alive": 1},          # союзник в ~1118
    ]
    hero_team = {"axe": 2, "kez": 3, "slardar": 3, "lion": 2}
    f = risk_features("npc_dota_hero_axe", 2, 200,
                      index_positions(positions), hero_team)
    assert f["enemies_in_1500"] == 1 and f["enemies_in_3000"] == 1
    assert f["alive_enemies"] == 1                      # мёртвый не считается
    assert f["allies_in_1500"] == 1 and f["alive_allies"] == 1
    assert 490 < f["dist_nearest_enemy"] < 510
    assert f["depth"] == (( (4000+4000)/(2*8000.0) + 1)/2)
    # позиций жертвы нет → None
    assert risk_features("npc_dota_hero_axe", 2, 999,
                         index_positions(positions), hero_team) is None
    # врагов не видно → FAR
    solo = [{"game_time": 200, "hero": "CDOTA_Unit_Hero_Axe",
             "x": 0, "y": 0, "is_alive": 1}]
    f2 = risk_features("npc_dota_hero_axe", 2, 200,
                       index_positions(solo), hero_team)
    assert f2["dist_nearest_enemy"] == RISK_FAR


def test_error_uses_model_risk_when_available():
    """risk_fn задан → SI в ошибке заменяется калиброванной вероятностью
    модели, в note — «риск-модель»; risk_fn вернул None → эвристика."""
    from reportgen.builder import index_positions, wp_attribution

    pts_wp = [{"game_time": t, "radiant_wp": w, "net_worth_diff": 0}
              for t, w in [(60, 0.5), (120, 0.5), (180, 0.5), (240, 0.3),
                           (300, 0.3)]]
    positions = []
    for t in range(180, 241, 10):
        positions.append({"game_time": t, "hero": "CDOTA_Unit_Hero_Axe",
                          "x": 6000, "y": 6000, "is_alive": 1})
        positions.append({"game_time": t, "hero": "CDOTA_Unit_Hero_Kez",
                          "x": 6100, "y": 6050, "is_alive": 1})
    kills = [{"game_time": 200, "target": "npc_dota_hero_axe",
              "attacker": "npc_dota_hero_kez"}]
    heroes = {"npc_dota_hero_axe": 0, "npc_dota_hero_kez": 5}
    teams = {0: 2, 5: 3}

    seen = []
    def model(feats):
        seen.append(feats)
        return 0.87
    errors, _ = wp_attribution(pts_wp, kills, heroes, teams,
                               positions_by_hero=index_positions(positions),
                               risk_fn=model)
    e = errors[0][0]
    assert e["safety_index"] == 0.87
    assert "риск-модель 0.87" in e["explanation"]
    assert seen and seen[0]["alive_enemies"] == 1.0

    errors2, _ = wp_attribution(pts_wp, kills, heroes, teams,
                                positions_by_hero=index_positions(positions),
                                risk_fn=lambda f: None)
    si2 = errors2[0][0]["safety_index"]            # фолбэк на эвристику
    assert si2 != 0.87 and 0 < si2 < 1
    assert "риск-модель" not in errors2[0][0]["explanation"]


def test_laning_uses_model_when_available():
    """laning_fn + early_combat → score = вероятность модели,
    laning_model=true; roam и отсутствие combat-лога — эвристика."""
    t = build_timeline(1, ROWS, WP)
    players = [
        dict(PLAYERS[0], lane="mid", lane_nw_diff_at_10=900,
             lh_at_5=30, dn_at_5=5),
        dict(PLAYERS[1], lane="roam", lane_nw_diff_at_10=0),
    ]
    early = {"npc_dota_hero_axe":
             {"dealt": 1500.0, "taken": 600.0, "kills": 1, "deaths": 0}}
    seen = []

    def model(feats):
        seen.append(feats)
        return 0.81

    a = build_analysis(1, "Dire", players, t, "v",
                       laning_fn=model, early_combat=early)
    p0, p1 = a["players"]
    assert p0["laning_score"] == 0.81 and p0["laning_model"] is True
    assert seen[0]["lh_at_5"] == 30.0 and seen[0]["is_mid"] == 1.0
    assert seen[0]["hero_dmg_dealt"] == 1500.0
    # roam — модель неприменима, эвристика (LH-прокси), флаг false
    assert p1["laning_model"] is False and 0 <= p1["laning_score"] <= 1
    assert len(seen) == 1


def test_laning_fallback_when_model_declines():
    """laning_fn вернул None (модель не сервится) → эвристика по
    lane_nw_diff_at_10, laning_model=false."""
    t = build_timeline(1, ROWS, WP)
    players = [dict(PLAYERS[0], lane="top", lane_nw_diff_at_10=1500,
                    lh_at_5=20, dn_at_5=2)]
    a = build_analysis(1, "Dire", players, t, "v",
                       laning_fn=lambda f: None,
                       early_combat={"npc_dota_hero_axe": {}})
    p0 = a["players"][0]
    assert p0["laning_model"] is False
    assert p0["laning_score"] == round(1 / (1 + 2.718281828 ** -1.0), 3)


def test_laning_without_model_keeps_heuristic():
    """Без laning_fn поведение прежнее (обратная совместимость)."""
    t = build_timeline(1, ROWS, WP)
    a = build_analysis(1, "Dire", PLAYERS, t, "v")
    for p in a["players"]:
        assert p["laning_model"] is False
