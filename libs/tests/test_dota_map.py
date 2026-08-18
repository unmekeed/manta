"""Тесты единой системы координат карты (спринт 139).

Проверяется свойство, ради которого модуль и заведён: ДВА независимых
источника координат должны попадать в одну точку подложки. Позиции
героев приходят из парсера в мировых единицах, варды — из OpenDota в
клетках, и до этого спринта они считались по трём несогласованным
константам в трёх файлах. Пока каждая карта рисовалась своей самодельной
схемой, расхождение было незаметно; на общей подложке оно станет видно
глазом.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dota_map as m  # noqa: E402


# -- согласованность двух пространств ----------------------------------------------

@pytest.mark.parametrize("cell,world", [
    (m.CELL_MIN, -m.WORLD_HALF),
    (128, 0.0),
    (m.CELL_MAX, m.WORLD_HALF),
])
def test_cell_bounds_map_to_world_bounds(cell, world):
    """Края клеточной сетки — это края мировой карты.

    Если 64 не совпадает с −WORLD_HALF, значит одна из систем считает
    карту другого размера, и всё, что нарисовано по обеим, разъедется.
    """
    assert m.cell_to_world(cell) == pytest.approx(world)


def test_both_systems_land_on_the_same_point():
    """ГЛАВНЫЙ тест модуля.

    Вард в клетке 64 и герой в мировой −WORLD_HALF стоят в одном углу
    карты. На единичной подложке они обязаны получить одну координату —
    иначе тепловая карта и варды нарисуются со сдвигом, и подложка
    сделает этот сдвиг видимым.
    """
    for cell in (m.CELL_MIN, 96, 128, 160, m.CELL_MAX):
        world = m.cell_to_world(cell)
        u_cell = m.cell_to_unit(cell, cell)
        u_world = m.world_to_unit(world, world)
        assert u_cell[0] == pytest.approx(u_world[0], abs=1e-9), cell
        assert u_cell[1] == pytest.approx(u_world[1], abs=1e-9), cell


def test_round_trip_cell_world():
    for c in (64.0, 77.5, 128.0, 191.9, 192.0):
        assert m.world_to_cell(m.cell_to_world(c)) == pytest.approx(c)


def test_units_per_cell_is_derived_not_typed():
    """Шаг клетки ВЫВОДИТСЯ из границ, а не записан отдельным числом.

    Записанный отдельно, он пережил бы правку WORLD_HALF и увёл бы две
    системы врозь молча — ровно так и появилось расхождение, которое
    этот модуль устраняет.
    """
    assert m.UNITS_PER_CELL == pytest.approx(
        2 * m.WORLD_HALF / (m.CELL_MAX - m.CELL_MIN))
    # Проверяем и следствие: смена границы тянет за собой шаг.
    assert m.cell_to_world(m.CELL_MAX) - m.cell_to_world(m.CELL_MIN) \
        == pytest.approx(2 * m.WORLD_HALF)


# -- единичное пространство --------------------------------------------------------

def test_origin_is_south_west():
    """(0,0) — юго-запад, база Radiant.

    Переворот оси Y для SVG делается ОДИН раз, в компоненте отрисовки.
    Если он случится ещё и здесь, карта выйдет правдоподобной, но
    зеркальной, и заметить это можно только зная, где обычно фармят.
    """
    assert m.world_to_unit(-m.WORLD_HALF, -m.WORLD_HALF) == (0.0, 0.0)
    assert m.world_to_unit(m.WORLD_HALF, m.WORLD_HALF) == (1.0, 1.0)
    assert m.world_to_unit(0.0, 0.0) == (0.5, 0.5)


def test_out_of_bounds_is_not_clamped():
    """За краем карты значения выходят за [0,1] и НЕ обрезаются.

    Обрезка превратила бы «точка вне карты» в «точка на краю карты», а
    это разные факты: первое — мусор или фонтан за пределами проходимой
    зоны, второе — законная позиция. Решать, что с этим делать, должен
    вызывающий.
    """
    u, v = m.world_to_unit(m.WORLD_HALF * 2, -m.WORLD_HALF * 2)
    assert u > 1.0 and v < 0.0
    assert not m.in_bounds(m.WORLD_HALF * 2, 0)
    assert m.in_bounds(0, 0)


# -- сетка -------------------------------------------------------------------------

def test_grid_edge_belongs_to_the_last_cell():
    """Точка ровно на краю — законная точка карты, а не (grid+1)-я клетка."""
    for grid in (16, 32, 64):
        assert m.unit_to_grid(1.0, 1.0, grid) == (grid - 1, grid - 1)
        assert m.unit_to_grid(0.0, 0.0, grid) == (0, 0)


def test_grid_is_uniform():
    """Клетки одинаковые: середина сетки приходится на середину карты."""
    for grid in (16, 32, 64):
        assert m.unit_to_grid(0.5, 0.5, grid) == (grid // 2, grid // 2)


def test_negative_unit_does_not_produce_negative_index():
    """Отрицательный индекс молча читался бы с конца массива."""
    gx, gy = m.unit_to_grid(-0.3, -0.3, 32)
    assert gx >= 0 and gy >= 0


# -- то, что модуль обязан заменить -------------------------------------------------

def test_half_side_is_not_half_diagonal():
    """Половина стороны и половина диагонали — разные величины.

    Именно их смешение и породило расхождение: в mapcells.py стоит
    комментарий «совпадает с MAP_HALF_DIAG», хотя одно из двух заведомо
    неверно — они отличаются в √2 раз.
    """
    half_diagonal = m.WORLD_HALF * math.sqrt(2)
    assert not math.isclose(half_diagonal, m.WORLD_HALF, rel_tol=0.01)
