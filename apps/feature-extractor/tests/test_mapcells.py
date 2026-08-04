"""Агрегат тепловых карт (спринт 98).

Проверяется то, что на глаз в карте не видно: сторона слоя, границы фаз
и отбрасывание невалидных координат. Карта — визуализация, и ошибка в
ней выглядит правдоподобно: зеркальная по сторонам или сжатая в одну
клетку картинка не отличается от настоящей, пока не сверишь с игрой.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor.mapcells import (GRID, MAP_HALF,  # noqa: E402
                                build_cells, cell, phase_of)

HERO_TEAM = {"npc_dota_hero_axe": 2, "npc_dota_hero_lina": 3}


def _by_kind(rows, kind, team=None):
    return [r for r in rows if r["kind"] == kind
            and (team is None or r["team"] == team)]


def test_phase_boundaries():
    assert phase_of(0) == "early"
    assert phase_of(599) == "early"
    assert phase_of(600) == "mid"
    assert phase_of(1499) == "mid"
    assert phase_of(1500) == "late"


def test_cell_maps_corners_and_centre():
    assert cell(-MAP_HALF, -MAP_HALF) == (0, 0)
    assert cell(MAP_HALF, MAP_HALF) == (GRID - 1, GRID - 1)
    assert cell(0, 0) == (GRID // 2, GRID // 2)


def test_cell_rejects_out_of_map_and_nan():
    assert cell(MAP_HALF * 2, 0) is None
    assert cell(float("nan"), 0) is None
    assert cell(None, 0) is None
    assert cell("нет", 0) is None


def test_death_belongs_to_the_victim_not_the_killer():
    """Сторона смерти — ПОГИБШЕГО. Перепутать значило бы получить карту,
    зеркальную по сторонам, и заметить это было бы почти нечем."""
    rows = build_cells([], [{"game_time": 300, "event_type": "KILL",
                             "x": 1000, "y": 1000,
                             "attacker": "npc_dota_hero_axe",
                             "target": "npc_dota_hero_lina"}],
                       [], HERO_TEAM)
    deaths = _by_kind(rows, "death")
    assert len(deaths) == 1
    assert deaths[0]["team"] == 3          # сторона Lina, а не Axe


def test_ward_belongs_to_the_placer():
    rows = build_cells([], [{"game_time": 300, "event_type": "WARD_PLACE",
                             "x": -2000, "y": 3000,
                             "attacker": "npc_dota_hero_axe",
                             "target": ""}], [], HERO_TEAM)
    assert _by_kind(rows, "ward")[0]["team"] == 2


def test_zero_coordinates_are_dropped():
    """До спринта 97 координаты событий были нулями. Без отбрасывания
    вся карта смертей собралась бы в центральную клетку и выглядела бы
    при этом правдоподобно."""
    rows = build_cells([], [{"game_time": 100, "event_type": "KILL",
                             "x": None, "y": None,
                             "attacker": "npc_dota_hero_axe",
                             "target": "npc_dota_hero_lina"}],
                       [], HERO_TEAM)
    assert _by_kind(rows, "death") == []


def _snap(t, hero, x, y, alive=1):
    return {"game_time": t, "hero": hero, "x": x, "y": y, "is_alive": alive}


def test_presence_counts_only_living_heroes():
    rows = build_cells([_snap(60, "npc_dota_hero_axe", 0, 0, alive=0)],
                       [], [], HERO_TEAM)
    assert _by_kind(rows, "presence") == []


def test_farm_excludes_positions_next_to_an_enemy():
    """Фарм — присутствие там, где рядом НЕТ врага. Именно это отличает
    фарм-маршрут от маршрута к драке."""
    together = build_cells(
        [_snap(60, "npc_dota_hero_axe", 0, 0),
         _snap(60, "npc_dota_hero_lina", 100, 100)], [], [], HERO_TEAM)
    assert _by_kind(together, "presence")
    assert _by_kind(together, "farm") == []

    apart = build_cells(
        [_snap(60, "npc_dota_hero_axe", -6000, -6000),
         _snap(60, "npc_dota_hero_lina", 6000, 6000)], [], [], HERO_TEAM)
    assert len(_by_kind(apart, "farm")) == 2


def test_fight_is_recorded_for_both_sides():
    """Карта отвечает на вопрос «где дрались», а не «кто победил» —
    исход лежит в MatchFights."""
    rows = build_cells([], [], [{"start_time": 1600, "x": 500, "y": 500}],
                       HERO_TEAM)
    fights = _by_kind(rows, "fight")
    assert {r["team"] for r in fights} == {2, 3}
    assert {r["phase"] for r in fights} == {"late"}


def test_repeat_positions_accumulate_in_one_cell():
    rows = build_cells([_snap(60, "npc_dota_hero_axe", 100, 100),
                        _snap(120, "npc_dota_hero_axe", 120, 120)],
                       [], [], HERO_TEAM)
    presence = _by_kind(rows, "presence", team=2)
    assert len(presence) == 1 and presence[0]["n"] == 2


def test_unknown_hero_is_skipped():
    """Герой вне ростера означает битый разбор, а не третью сторону."""
    rows = build_cells([_snap(60, "npc_dota_hero_pudge", 0, 0)],
                       [], [], HERO_TEAM)
    assert rows == []


def test_rows_carry_no_match_id():
    """match_id ставит раннер — модуль остаётся чистой функцией и
    тестируется без ClickHouse."""
    rows = build_cells([_snap(60, "npc_dota_hero_axe", 0, 0)], [], [],
                       HERO_TEAM)
    assert rows and "match_id" not in rows[0]
