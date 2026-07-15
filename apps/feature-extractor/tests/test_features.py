"""Тесты расчёта фич на синтетическом матче 2×1 (по одному игроку в команде)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extractor.features import Roster, player_features, timeline_features

PLAYERS = [
    {"team": 2, "name": "R1", "hero": "npc_dota_hero_axe"},
    {"team": 3, "name": "D1", "hero": "npc_dota_hero_kez"},
]


def economy_rows():
    """Пик-фаза до t=120 (нули), затем линейный фарм: Radiant быстрее."""
    rows = []
    for t in range(0, 1321, 10):
        active = max(0, t - 120)  # игровые секунды после старта
        rows.append({"player_id": 0, "game_time": t,
                     "net_worth": active * 10, "total_gold": active * 10,
                     "total_xp": active * 12, "lh": active // 10, "dn": 1})
        rows.append({"player_id": 5, "game_time": t,
                     "net_worth": active * 8, "total_gold": active * 8,
                     "total_xp": active * 9, "lh": active // 15, "dn": 0})
    return rows


def test_roster_mapping():
    r = Roster.from_players(PLAYERS, "Dire")
    assert r.teams == {0: 2, 5: 3}
    assert r.hero_team["npc_dota_hero_kez"] == 3
    assert r.winner == 3


def test_player_features_windows_and_rates():
    r = Roster.from_players(PLAYERS, "Radiant")
    rows = player_features(economy_rows(), r, duration_s=1200)
    by_pid = {row["player_id"]: row for row in rows}
    p0 = by_pid[0]
    # Старт игры определён по первому ненулевому сэмплу (t=130):
    # окно "5 минут" = 130 + 300 = 430 → active = 310 → lh = 31.
    assert p0["lh_at_5"] == 31
    assert p0["net_worth_at_10"] == (130 + 600 - 120) * 10
    # GPM: total_gold финального сэмпла (active=1200) / 20 минут.
    assert p0["gpm"] == (1200 * 10) / 20
    assert p0["won"] == 1 and by_pid[5]["won"] == 0
    # Доли net worth в команде из одного игрока — 1.0.
    assert p0["gold_share"] == 1.0


def test_timeline_diff_and_kills():
    r = Roster.from_players(PLAYERS, "Dire")
    kills = [
        {"game_time": 200, "target": "npc_dota_hero_kez"},   # убил Radiant
        {"game_time": 400, "target": "npc_dota_hero_axe"},   # убил Dire
        {"game_time": 500, "target": "npc_dota_hero_axe"},
    ]
    rows = timeline_features(economy_rows(), kills, r)
    by_t = {row["game_time"]: row for row in rows}
    # t=60: пик-фаза, дифференциалы нулевые.
    assert by_t[60]["networth_diff"] == 0
    # t=600: active=480 → diff = 480*10 - 480*8 = 960.
    assert by_t[600]["networth_diff"] == 960
    assert by_t[300]["kills_radiant"] == 1 and by_t[300]["kills_dire"] == 0
    assert by_t[600]["kills_dire"] == 2
    assert all(row["radiant_win"] == 0 for row in rows)
    # Точки идут шагом 60 с до последнего сэмпла.
    assert rows[0]["game_time"] == 60 and rows[-1]["game_time"] == 1320


def test_point_in_time_no_leakage():
    """Окно N минут не должно видеть значения будущих сэмплов."""
    r = Roster.from_players(PLAYERS, "Radiant")
    econ = economy_rows()
    rows = player_features(econ, r, duration_s=1200)
    p0 = next(row for row in rows if row["player_id"] == 0)
    final_lh = max(row["lh"] for row in econ if row["player_id"] == 0)
    assert p0["lh_at_5"] < p0["lh_at_10"] < final_lh


def test_position_advance_windows_and_gaps():
    from extractor.features import position_advance_by_window

    positions = [
        # окно 60: двое у базы Radiant (-8000,-8000) и в центре.
        {"game_time": 30, "x": -8000, "y": -8000},
        {"game_time": 50, "x": 0, "y": 0},
        # окно 180 (в 120 снапшотов нет): у базы Dire.
        {"game_time": 170, "x": 8000, "y": 8000},
        # за пределами max_t — игнор.
        {"game_time": 500, "x": 8000, "y": 8000},
    ]
    adv = position_advance_by_window(positions, max_t=240)
    assert adv[60] == -0.5           # среднее(-1, 0)
    assert adv[120] == -0.5          # пропуск наследует последнее значение
    assert adv[180] == 1.0
    assert adv[240] == 1.0
    # Пустые позиции → все окна 0 (старые матчи без снапшотов).
    assert position_advance_by_window([], 120) == {60: 0.0, 120: 0.0}


def test_timeline_includes_position_advance():
    r = Roster.from_players(PLAYERS, "Dire")
    positions = [{"game_time": t, "x": 4000, "y": 4000}
                 for t in range(10, 1321, 50)]
    rows = timeline_features(economy_rows(), [], r, positions=positions)
    assert all(row["position_advance"] == 0.5 for row in rows)
    rows0 = timeline_features(economy_rows(), [], r)
    assert all(row["position_advance"] == 0.0 for row in rows0)
