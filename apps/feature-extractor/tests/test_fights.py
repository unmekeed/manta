"""Восстановление драк из комбат-лога (A14, спринт 84).

Таблица заводится ради TTL: ReplayEvents живёт 14 дней и это
единственный источник размена. Проверяется, что кластеризация не
склеивает разные стычки, не разрывает одну, и что исход считается по
размену, а не по числу участников.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor.fights import FIGHT_GAP_S, detect_fights  # noqa: E402

HERO_TEAM = {
    "npc_dota_hero_axe": 2, "npc_dota_hero_lina": 2, "npc_dota_hero_sven": 2,
    "npc_dota_hero_mirana": 2, "npc_dota_hero_pudge": 2,
    "npc_dota_hero_lion": 3, "npc_dota_hero_juggernaut": 3,
    "npc_dota_hero_tiny": 3, "npc_dota_hero_zuus": 3, "npc_dota_hero_ursa": 3,
}
R = [h for h, t in HERO_TEAM.items() if t == 2]
D = [h for h, t in HERO_TEAM.items() if t == 3]


def _kill(t, target, attacker="npc_dota_hero_lion"):
    return {"game_time": t, "target": target, "attacker": attacker}


def _pos(t, hero, x, y, alive=1):
    return {"game_time": t, "hero": hero, "x": x, "y": y, "is_alive": alive}


def _all_near(t, x=1000.0, y=1000.0):
    return [_pos(t, h, x + i * 50, y) for i, h in enumerate(R + D)]


def test_deaths_close_in_time_form_one_fight():
    kills = [_kill(600, R[0]), _kill(605, D[0]), _kill(612, R[1])]
    fights = detect_fights(kills, _all_near(600), HERO_TEAM)
    assert len(fights) == 1
    f = fights[0]
    assert f["start_time"] == 600 and f["end_time"] == 612
    assert f["radiant_deaths"] == 2 and f["dire_deaths"] == 1


def test_gap_splits_into_separate_fights():
    """Разрыв больше FIGHT_GAP_S — разные стычки, а не одна длинная."""
    kills = [_kill(600, R[0]), _kill(600 + FIGHT_GAP_S + 5, D[0])]
    fights = detect_fights(kills, _all_near(600) + _all_near(630), HERO_TEAM)
    assert len(fights) == 2
    assert [f["fight_id"] for f in fights] == [0, 1]


def test_outcome_follows_trade_not_participants():
    """«Выиграл драку» = потерял меньше. Численный перевес на исход в
    этой метрике не влияет — иначе мы бы кодировали в неё свою же
    гипотезу вместо факта."""
    kills = [_kill(600, D[0]), _kill(603, D[1]), _kill(606, R[0])]
    f = detect_fights(kills, _all_near(600), HERO_TEAM)[0]
    assert f["outcome"] == 1                 # Radiant разменял выгоднее
    assert f["radiant_deaths"] == 1 and f["dire_deaths"] == 2


def test_even_trade_is_draw():
    kills = [_kill(600, R[0]), _kill(602, D[0])]
    assert detect_fights(kills, _all_near(600), HERO_TEAM)[0]["outcome"] == 0


def test_participants_include_the_dead():
    """Погибший к моменту замера уже мёртв и в подсчёт живых не попадёт,
    но в драке он участвовал."""
    kills = [_kill(600, R[0])]
    pos = _all_near(600)
    # помечаем погибшего мёртвым — как это и приходит из парсера
    for p in pos:
        if p["hero"] == R[0]:
            p["is_alive"] = 0
    f = detect_fights(kills, pos, HERO_TEAM)[0]
    assert f["radiant_participants"] == 5     # 4 живых рядом + погибший


def test_far_heroes_are_not_participants():
    """Ровно то, ради чего всё затевалось: трое дерутся, двое на другом
    краю карты — они не участники."""
    kills = [_kill(600, R[0])]
    pos = [_pos(600, h, 1000 + i * 50, 1000) for i, h in enumerate(R[:3])]
    pos += [_pos(600, h, -7000, -7000) for h in R[3:]]
    pos += [_pos(600, h, 1200 + i * 50, 1000) for i, h in enumerate(D)]
    f = detect_fights(kills, pos, HERO_TEAM)[0]
    # трое рядом, из них один погиб (он же и в списке живых не окажется)
    assert f["radiant_participants"] <= 3
    assert f["dire_participants"] == 5


def test_center_is_where_deaths_happened():
    kills = [_kill(600, R[0]), _kill(604, R[1])]
    pos = [_pos(600, R[0], 2000, 2000), _pos(604, R[1], 2200, 2000)]
    f = detect_fights(kills, pos, HERO_TEAM)[0]
    assert 2000 <= f["x"] <= 2200 and abs(f["y"] - 2000) < 1


def test_no_positions_omits_coords_but_keeps_trade():
    """Позиций нет — координаты НЕ пишутся (сработает DEFAULT nan;
    json.dumps(nan) уронил бы вставку), но размен сохраняем: он и есть
    главное, что стирается вместе с TTL."""
    f = detect_fights([_kill(600, R[0]), _kill(603, D[0])], [], HERO_TEAM)[0]
    assert "x" not in f and "y" not in f
    assert f["radiant_deaths"] == 1 and f["dire_deaths"] == 1


def test_rows_are_json_serializable():
    """Каждая строка обязана пережить json.dumps: вставка идёт
    JSONEachRow, и один NaN положил бы всю пачку."""
    import json
    rows = detect_fights([_kill(600, R[0]), _kill(603, D[0])], [], HERO_TEAM)
    rows += detect_fights([_kill(700, R[0])], _all_near(700), HERO_TEAM)
    for r in rows:
        assert "NaN" not in json.dumps(r)


def test_no_kills_no_fights():
    assert detect_fights([], _all_near(600), HERO_TEAM) == []


def test_creep_and_building_kills_ignored():
    """В драки попадают только смерти ГЕРОЕВ."""
    kills = [{"game_time": 600, "target": "npc_dota_creep_badguys_melee",
              "attacker": R[0]},
             {"game_time": 601, "target": "npc_dota_badguys_tower1_mid",
              "attacker": R[0]}]
    assert detect_fights(kills, _all_near(600), HERO_TEAM) == []


def test_fight_ids_are_sequential():
    kills = []
    for i in range(3):
        kills.append(_kill(600 + i * (FIGHT_GAP_S + 10), R[0]))
    fights = detect_fights(kills, [], HERO_TEAM)
    assert [f["fight_id"] for f in fights] == [0, 1, 2]
