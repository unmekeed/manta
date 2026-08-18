"""Агрегат тепловых карт (спринт 98).

Проверяется то, что на глаз в карте не видно: сторона слоя, границы фаз
и отбрасывание невалидных координат. Карта — визуализация, и ошибка в
ней выглядит правдоподобно: зеркальная по сторонам или сжатая в одну
клетку картинка не отличается от настоящей, пока не сверишь с игрой.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "libs"))

# Граница карты берётся из общего модуля, а не из mapcells: у величины
# должно быть ОДНО имя и одно место. Раньше mapcells держал собственную
# WORLD_HALF = 8000, скопированную из нормировки диагонали, — и именно так
# в проекте завелись три несогласованных представления карты.
from dota_map import WORLD_HALF  # noqa: E402

from extractor.mapcells import (GRID,  # noqa: E402
                                build_cells, cell, core_heroes, phase_of)

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
    assert cell(-WORLD_HALF, -WORLD_HALF) == (0, 0)
    assert cell(WORLD_HALF, WORLD_HALF) == (GRID - 1, GRID - 1)
    assert cell(0, 0) == (GRID // 2, GRID // 2)


def test_cell_rejects_out_of_map_and_nan():
    assert cell(WORLD_HALF * 2, 0) is None
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


# -- фарм коров (спринт 140) ---------------------------------------------------

def _economy(pairs):
    """pairs: [(player_id, lh)] — по два сэмпла на игрока, чтобы
    проверялось, что берётся ПОСЛЕДНИЙ, а не первый попавшийся."""
    rows = []
    for pid, lh in pairs:
        rows.append({"player_id": pid, "game_time": 60, "lh": 0})
        rows.append({"player_id": pid, "game_time": 1800, "lh": lh})
    return rows


TEAMS_5V5 = {0: 2, 1: 2, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 3, 8: 3, 9: 3}
HEROES_5V5 = {i: f"npc_dota_hero_h{i}" for i in range(10)}


def test_cores_are_the_top_three_by_last_hits():
    """Граница «кор / саппорт» проходит после ТРЕТЬЕГО места по добиткам."""
    lh = [(0, 300), (1, 250), (2, 200), (3, 40), (4, 20),
          (5, 310), (6, 260), (7, 210), (8, 30), (9, 10)]
    cores = core_heroes(_economy(lh), TEAMS_5V5, HEROES_5V5)
    assert cores == {f"h{i}" for i in (0, 1, 2, 5, 6, 7)}


def test_cores_are_taken_per_team_not_globally():
    """Три кора У КАЖДОЙ стороны, а не три лучших в матче.

    У разгромленной команды все пятеро могут добить меньше, чем любой из
    победителей, — и глобальный отбор оставил бы её вовсе без коров, то
    есть без карты фарма.
    """
    lh = [(0, 500), (1, 480), (2, 460), (3, 440), (4, 420),
          (5, 90), (6, 70), (7, 50), (8, 30), (9, 10)]
    cores = core_heroes(_economy(lh), TEAMS_5V5, HEROES_5V5)
    assert cores == {f"h{i}" for i in (0, 1, 2, 5, 6, 7)}


def test_cores_use_the_last_sample_not_the_first():
    """На первой минуте добиток нет ни у кого; ранг по началу матча был бы
    ранжированием нулей."""
    lh = [(0, 5), (1, 400), (2, 380), (3, 360), (4, 4),
          (5, 1), (6, 2), (7, 3), (8, 4), (9, 5)]
    cores = core_heroes(_economy(lh), TEAMS_5V5, HEROES_5V5)
    assert {f"h{i}" for i in (1, 2, 3)} <= cores


def test_core_ranking_is_stable_when_last_hits_tie():
    """Одинаковые добитки не должны давать РАЗНЫЕ карты при переразборе.

    Перемешивается именно РОСТЕР, а не экономика: сортировка идёт по
    игрокам, и без явного второго ключа результат зависел бы от порядка
    обхода словаря. Экономика на это не влияет вовсе — первая версия
    теста переставляла её и потому не замечала, что второй ключ убрали.

    Порядок ростера не выдуман: он собирается из payload разбора, и у
    бэкфилла игроки приходят не тем же порядком, что у свежего матча.
    """
    lh = [(i, 100) for i in range(10)]
    economy = _economy(lh)
    first = core_heroes(economy, TEAMS_5V5, HEROES_5V5)
    assert first, "при равных добитках коры всё равно должны быть"
    for order in (list(reversed(range(10))), [5, 2, 8, 0, 9, 1, 7, 3, 6, 4]):
        teams = {i: TEAMS_5V5[i] for i in order}
        heroes = {i: HEROES_5V5[i] for i in order}
        assert core_heroes(economy, teams, heroes) == first


def test_farm_core_counts_cores_only():
    """Саппорт в безопасной зоне попадает в 'farm', но не в 'farm_core'."""
    hero_team = {"npc_dota_hero_carry": 2, "npc_dota_hero_supp": 2,
                 "npc_dota_hero_lina": 3}
    rows = build_cells(
        [_snap(60, "npc_dota_hero_carry", -6000, -6000),
         _snap(60, "npc_dota_hero_supp", -5000, -5000),
         _snap(60, "npc_dota_hero_lina", 6000, 6000)],
        [], [], hero_team, cores={"npc_dota_hero_carry"})
    assert len(_by_kind(rows, "farm", team=2)) == 2
    core = _by_kind(rows, "farm_core", team=2)
    assert len(core) == 1
    assert (core[0]["gx"], core[0]["gy"]) == cell(-6000, -6000)


def test_farm_core_is_absent_when_cores_are_unknown():
    """Без списка коров вид не пишется ВОВСЕ.

    Посчитанный по всем пятерым, он был бы неотличим от честного и врал
    бы молча — а отсутствующий ключ читается однозначно.
    """
    rows = build_cells(
        [_snap(60, "npc_dota_hero_axe", -6000, -6000),
         _snap(60, "npc_dota_hero_lina", 6000, 6000)], [], [], HERO_TEAM)
    assert _by_kind(rows, "farm")
    assert _by_kind(rows, "farm_core") == []


def test_farm_core_never_exceeds_farm():
    """Коры — подмножество команды, значит и клетки их фарма — подмножество.

    Обратное означало бы, что кор посчитан дважды или приписан не своей
    стороне.
    """
    hero_team = {"npc_dota_hero_carry": 2, "npc_dota_hero_supp": 2,
                 "npc_dota_hero_lina": 3}
    rows = build_cells(
        [_snap(60, "npc_dota_hero_carry", -6000, -6000),
         _snap(120, "npc_dota_hero_carry", -6000, -6000),
         _snap(60, "npc_dota_hero_supp", -6000, -6000),
         _snap(60, "npc_dota_hero_lina", 6000, 6000)],
        [], [], hero_team, cores={"npc_dota_hero_carry"})
    farm = {(r["phase"], r["gx"], r["gy"]): r["n"]
            for r in _by_kind(rows, "farm", team=2)}
    for r in _by_kind(rows, "farm_core", team=2):
        key = (r["phase"], r["gx"], r["gy"])
        assert key in farm, "клетка farm_core без соответствующей farm"
        assert r["n"] <= farm[key]
