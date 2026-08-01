"""Локальный перевес и собранность команды (A14, спринт 83).

Мотив: `alive_diff` считает живых по ВСЕЙ карте, поэтому «пятеро против
пятерых, но трое наших на другом краю» для модели выглядит как
равенство. Здесь проверяется, что новые фичи это различают.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor.features import (CONTACT_R, local_manpower_by_window,  # noqa: E402
                                spread_by_window, timeline_features)

HERO_TEAM = {
    "npc_dota_hero_axe": 2, "npc_dota_hero_lina": 2, "npc_dota_hero_sven": 2,
    "npc_dota_hero_mirana": 2, "npc_dota_hero_pudge": 2,
    "npc_dota_hero_lion": 3, "npc_dota_hero_juggernaut": 3,
    "npc_dota_hero_tiny": 3, "npc_dota_hero_zuus": 3, "npc_dota_hero_ursa": 3,
}
RADIANT = [h for h, t in HERO_TEAM.items() if t == 2]
DIRE = [h for h, t in HERO_TEAM.items() if t == 3]


def _snap(t, hero, x, y, alive=1):
    return {"game_time": t, "hero": hero, "x": x, "y": y, "is_alive": alive}


def test_three_versus_five_is_detected():
    """Ровно случай из постановки: у Radiant трое в драке, у Dire пятеро,
    хотя живых поровну."""
    pos = []
    # трое Radiant у точки боя
    for i, h in enumerate(RADIANT[:3]):
        pos.append(_snap(60, h, 100 + i * 50, 100))
    # двое Radiant на другом конце карты — живы, но не участвуют
    for h in RADIANT[3:]:
        pos.append(_snap(60, h, -7000, -7000))
    # все пятеро Dire рядом
    for i, h in enumerate(DIRE):
        pos.append(_snap(60, h, 300 + i * 50, 200))

    out = local_manpower_by_window(pos, HERO_TEAM, 60)
    assert out[60] == -2.0, out          # 3 против 5


def test_full_map_parity_differs_from_local_deficit():
    """Контраст с alive_diff: живых поровну (5:5), но локально −2."""
    from extractor.features import alive_diff_by_window
    pos = []
    for i, h in enumerate(RADIANT[:3]):
        pos.append(_snap(60, h, 100 + i * 50, 100))
    for h in RADIANT[3:]:
        pos.append(_snap(60, h, -7000, -7000))
    for i, h in enumerate(DIRE):
        pos.append(_snap(60, h, 300 + i * 50, 200))

    assert alive_diff_by_window(pos, HERO_TEAM, 60)[60] == 0.0
    assert local_manpower_by_window(pos, HERO_TEAM, 60)[60] == -2.0


def test_no_contact_gives_zero():
    """Стороны разведены по карте — никто не дерётся. Ноль здесь верен:
    это не «силы равны», а «стычки нет»."""
    pos = [_snap(60, h, -7000, -7000) for h in RADIANT]
    pos += [_snap(60, h, 7000, 7000) for h in DIRE]
    assert local_manpower_by_window(pos, HERO_TEAM, 60)[60] == 0.0


def test_dead_heroes_do_not_count():
    """Мёртвый герой рядом с дракой в ней не участвует."""
    pos = [_snap(60, h, 100, 100) for h in RADIANT[:2]]
    pos.append(_snap(60, RADIANT[2], 120, 100, alive=0))
    pos += [_snap(60, h, 200, 100) for h in DIRE[:2]]
    assert local_manpower_by_window(pos, HERO_TEAM, 60)[60] == 0.0


def test_contact_radius_boundary():
    """За пределами CONTACT_R контакта нет."""
    far = CONTACT_R + 100
    pos = [_snap(60, RADIANT[0], 0, 0), _snap(60, DIRE[0], far, 0)]
    assert local_manpower_by_window(pos, HERO_TEAM, 60)[60] == 0.0
    near = [_snap(60, RADIANT[0], 0, 0), _snap(60, DIRE[0], CONTACT_R - 100, 0)]
    assert local_manpower_by_window(near, HERO_TEAM, 60)[60] == 0.0  # 1 на 1


def test_spread_distinguishes_grouped_from_scattered():
    """Radiant кучей, Dire размазан → отрицательный spread_diff."""
    pos = [_snap(60, h, 100 + i * 20, 100) for i, h in enumerate(RADIANT)]
    pos += [_snap(60, h, i * 3000 - 6000, i * 2000 - 4000)
            for i, h in enumerate(DIRE)]
    out = spread_by_window(pos, HERO_TEAM, 60)
    assert out[60] < -0.1, out


def test_spread_symmetric_when_both_grouped():
    pos = [_snap(60, h, 100 + i * 20, 100) for i, h in enumerate(RADIANT)]
    pos += [_snap(60, h, 500 + i * 20, 100) for i, h in enumerate(DIRE)]
    assert abs(spread_by_window(pos, HERO_TEAM, 60)[60]) < 1e-6


def _economy(minutes: int):
    rows = []
    for t in range(0, minutes * 60 + 1, 10):
        for pid in range(10):
            rows.append({"player_id": pid, "game_time": t,
                         "net_worth": 500 + t, "total_gold": 500 + t,
                         "total_xp": 400 + t, "lh": t // 10, "dn": 0})
    return rows


def test_timeline_omits_features_without_positions():
    """Без позиций ключи НЕ пишутся — в ClickHouse сработает DEFAULT nan.
    Ноль означал бы «стороны сошлись поровну», то есть ложный сигнал."""
    from extractor.features import Roster
    players = [{"team": 2, "hero": h, "name": h} for h in RADIANT]
    players += [{"team": 3, "hero": h, "name": h} for h in DIRE]
    roster = Roster.from_players(players, "Radiant")

    rows = timeline_features(_economy(3), [], roster, positions=None)
    assert rows
    assert "local_manpower_diff" not in rows[0]
    assert "spread_diff" not in rows[0]


def test_timeline_includes_features_with_positions():
    from extractor.features import Roster
    players = [{"team": 2, "hero": h, "name": h} for h in RADIANT]
    players += [{"team": 3, "hero": h, "name": h} for h in DIRE]
    roster = Roster.from_players(players, "Radiant")

    pos = []
    for t in (60, 120, 180):
        for i, h in enumerate(RADIANT):
            pos.append(_snap(t, h, 100 + i * 30, 100))
        for i, h in enumerate(DIRE):
            pos.append(_snap(t, h, 400 + i * 30, 100))

    rows = timeline_features(_economy(3), [], roster, positions=pos)
    assert rows
    assert "local_manpower_diff" in rows[0]
    assert "spread_diff" in rows[0]
