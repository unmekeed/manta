"""Поминутный агрегат тепловых карт (спринт 147).

ЗАЧЕМ. Три фазы отвечают на вопрос «где команда была в середине игры», а
от карты хотят маршрута. Между десятой и двадцать пятой минутой керри
успевает пройти лес, вернуться на линию и уйти в чужой лес — на карте
фазы 'mid' это один развод пятен, из которого порядок обхода не
восстановить.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ЧЕГО РАДИ. Главное — что фазовая карта осталась
РОВНО такой же. Поминутный слой заводится не вместо неё, а под ней:
считается всё один раз поминутно, а фазовые строки выводятся
суммированием. Если суммирование где-то теряет или дублирует попадания,
все прежние отчёты тихо изменят числа — а тихо изменившаяся тепловая
карта неотличима от настоящей.

Второе — что границы фаз ложатся на целые минуты. Сдвинь EARLY_END на
полминуты, и суммирование припишет минуту целиком не той фазе; разница
будет в единицы процентов, то есть глазом не видна.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "libs"))

from extractor.mapcells import (EARLY_END, MID_END,  # noqa: E402
                                SECONDS_PER_MINUTE, build_cells,
                                build_cells_by_minute, minute_of, phase_cells,
                                phase_of)

HERO_TEAM = {"npc_dota_hero_axe": 2, "npc_dota_hero_lina": 3}


def _pos(t, hero, x, y, alive=1):
    return {"game_time": t, "hero": hero, "x": x, "y": y, "is_alive": alive}


def _economy(pid, samples):
    """Экономика игрока: [(время, добитки), …]."""
    return [{"player_id": pid, "game_time": t, "lh": lh} for t, lh in samples]


# -- сама минута --------------------------------------------------------------

def test_minute_of_counts_whole_minutes():
    assert minute_of(0) == 0
    assert minute_of(59) == 0
    assert minute_of(60) == 1
    assert minute_of(1499) == 24
    assert minute_of(1500) == 25


def test_time_before_the_horn_falls_into_minute_zero():
    """Отрицательное время — стадия закупки, а не минута «минус один».

    UInt16 в схеме отрицательных не примет вовсе, и запись матча упала бы
    целиком. Прежняя разметка относила это время к 'early', то есть тоже
    к началу.
    """
    assert minute_of(-90) == 0
    assert minute_of(-1) == 0
    assert minute_of(None) == 0


def test_phase_boundaries_fall_on_whole_minutes():
    """Ни одна минута не делится между фазами.

    На этом стоит суммирование: минута целиком принадлежит одной фазе.
    Сдвинь границу на полминуты — и фазовая карта начнёт отличаться от
    прежней на единицы процентов, то есть незаметно.
    """
    assert EARLY_END % SECONDS_PER_MINUTE == 0
    assert MID_END % SECONDS_PER_MINUTE == 0


# -- согласие двух зернистостей ----------------------------------------------

def _sample_match():
    """Матч с попаданиями во все три фазы и обе стороны."""
    positions = []
    economy = []
    lh = 0
    for t in range(0, 1800, 60):
        # Оба героя стоят в разных местах и оба фармят: добитки растут.
        positions.append(_pos(t, "npc_dota_hero_axe", -3000 + t, -3000))
        positions.append(_pos(t, "npc_dota_hero_lina", 3000 - t, 3000))
        economy += _economy(1, [(t, lh)]) + _economy(2, [(t, lh)])
        lh += 1
    events = [
        {"game_time": 120, "event_type": "KILL", "x": 0, "y": 0,
         "target": "npc_dota_hero_axe"},
        {"game_time": 900, "event_type": "WARD_PLACE", "x": 1000, "y": 1000,
         "attacker": "npc_dota_hero_lina"},
        {"game_time": 1600, "event_type": "SMOKE", "x": -1000, "y": -1000,
         "attacker": "npc_dota_hero_axe"},
    ]
    fights = [{"start_time": 1000, "x": 500, "y": -500}]
    heroes = {1: "npc_dota_hero_axe", 2: "npc_dota_hero_lina"}
    cores = {"npc_dota_hero_axe", "npc_dota_hero_lina"}
    return positions, events, fights, economy, heroes, cores


def test_phase_map_is_exactly_the_sum_of_its_minutes():
    """ГЛАВНОЕ утверждение спринта.

    Фазовые строки теперь выводятся из поминутных, а не считаются вторым
    проходом. Потеряйся хоть одно попадание — прежние отчёты изменят
    числа молча.
    """
    positions, events, fights, economy, heroes, cores = _sample_match()
    minutes = build_cells_by_minute(positions, events, fights, HERO_TEAM,
                                    cores, economy=economy, heroes=heroes)
    phases = phase_cells(minutes)

    assert sum(r["n"] for r in minutes) == sum(r["n"] for r in phases), \
        "суммарное число попаданий разошлось"
    for kind in ("presence", "farm", "farm_core", "death", "ward",
                 "smoke", "fight"):
        a = sum(r["n"] for r in minutes if r["kind"] == kind)
        b = sum(r["n"] for r in phases if r["kind"] == kind)
        assert a == b, f"{kind}: поминутно {a}, по фазам {b}"


def test_build_cells_still_returns_phase_rows():
    """Старый вход не изменился ни по подписи, ни по результату.

    У build_cells есть потребители помимо раннера (backfill_farm_core), и
    смена формы вывода сломала бы их молча: строка без 'phase' просто не
    вставится, а вставка идёт в try/except.
    """
    positions, events, fights, economy, heroes, cores = _sample_match()
    rows = build_cells(positions, events, fights, HERO_TEAM, cores,
                       economy=economy, heroes=heroes)
    assert rows, "карта пуста — проверять нечего"
    assert all(set(r) == {"phase", "team", "kind", "gx", "gy", "n"}
               for r in rows)
    assert {r["phase"] for r in rows} <= {"early", "mid", "late"}


def test_minute_rows_carry_a_minute_and_no_phase():
    """Поминутная строка описана СВОИМ ключом.

    Оставить в ней и phase значило бы завести второе имя для того же
    факта: минута 12 всегда 'mid', и расходиться им негде — но лишнее
    поле неизбежно однажды заполнят иначе.
    """
    positions, events, fights, economy, heroes, cores = _sample_match()
    rows = build_cells_by_minute(positions, events, fights, HERO_TEAM, cores,
                                 economy=economy, heroes=heroes)
    assert rows
    assert all(set(r) == {"minute", "team", "kind", "gx", "gy", "n"}
               for r in rows)
    assert all(isinstance(r["minute"], int) and r["minute"] >= 0 for r in rows)


def test_minutes_are_distinguished_and_not_collapsed():
    """Соседние минуты — РАЗНЫЕ строки, даже в одной клетке.

    Ради этого всё и затевалось: схлопни их, и получится фазовая карта
    под другим именем. Герой стоит на месте две минуты подряд — обязаны
    появиться две строки.
    """
    positions = [_pos(t, "npc_dota_hero_axe", -3000, -3000)
                 for t in (30, 90)]
    rows = build_cells_by_minute(positions, [], [], HERO_TEAM)
    presence = [r for r in rows if r["kind"] == "presence"]
    assert sorted(r["minute"] for r in presence) == [0, 1]


def test_route_is_readable_minute_by_minute():
    """Проверка ради чего: по минутам виден ПОРЯДОК обхода.

    Герой идёт из нижнего левого угла в верхний правый. В фазовой карте
    это два пятна без указания, что было раньше; поминутно клетка растёт
    монотонно вместе с минутой.
    """
    positions = [_pos(m * 60, "npc_dota_hero_axe", -6000 + m * 1500, -6000)
                 for m in range(6)]
    rows = build_cells_by_minute(positions, [], [], HERO_TEAM)
    track = sorted((r["minute"], r["gx"]) for r in rows
                   if r["kind"] == "presence")
    assert [m for m, _ in track] == [0, 1, 2, 3, 4, 5]
    xs = [gx for _, gx in track]
    assert xs == sorted(xs) and xs[0] < xs[-1], "маршрут не читается"


def test_phase_cells_survives_empty_input():
    assert phase_cells([]) == []
    assert phase_cells(None) == []


def test_phase_cells_assigns_minutes_to_the_right_phase():
    """Минута попадает в ту фазу, что и её начало.

    Проверяются ровно граничные минуты: 9 — последняя ранняя, 10 — первая
    средняя, 24 — последняя средняя, 25 — первая поздняя.
    """
    rows = [{"minute": m, "team": 2, "kind": "presence", "gx": 1, "gy": 1,
             "n": 1} for m in (9, 10, 24, 25)]
    got = {r["phase"]: r["n"] for r in phase_cells(rows)}
    assert got == {"early": 1, "mid": 2, "late": 1}
    # И то же самое, выраженное через phase_of: если границы уедут,
    # разъедутся обе проверки разом, а не одна.
    assert phase_of(9 * 60) == "early" and phase_of(10 * 60) == "mid"
    assert phase_of(24 * 60) == "mid" and phase_of(25 * 60) == "late"
