"""Тепловые карты в отчёте (спринт 110).

Запрос владельца: карты присутствия, фарм-маршруты, варды, смоки,
смерти и важные драки по фазам игры. Спринты 97–99 добывали сырьё,
здесь оно становится секцией отчёта.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from reportgen.heatmaps import (build_heatmaps,  # noqa: E402
                                heatmaps_available)


def _cell(phase="early", team=2, kind="presence", gx=1, gy=2, n=5):
    return {"phase": phase, "team": team, "kind": kind,
            "gx": gx, "gy": gy, "n": n}


def test_cells_split_by_phase_kind_and_side():
    out = build_heatmaps([
        _cell(phase="early", team=2, kind="presence", gx=1, gy=1, n=3),
        _cell(phase="early", team=3, kind="presence", gx=2, gy=2, n=4),
        _cell(phase="late", team=2, kind="smoke", gx=5, gy=5, n=1),
    ])
    early = out["phases"]["early"]["presence"]
    assert early["radiant"] == [[1, 1, 3]]
    assert early["dire"] == [[2, 2, 4]]
    assert out["phases"]["late"]["smoke"]["radiant"] == [[5, 5, 1]]


def test_max_is_per_kind_not_per_map():
    """Присутствие даёт сотни попаданий в клетку, смоки — единицы. Общая
    шкала превратила бы карту смоков в пустой лист: она бы честно
    рисовалась, просто вся в цвете «почти ноль»."""
    out = build_heatmaps([
        _cell(kind="presence", n=400),
        _cell(kind="smoke", gx=3, n=2),
    ])
    assert out["phases"]["early"]["presence"]["max_n"] == 400
    assert out["phases"]["early"]["smoke"]["max_n"] == 2


def test_max_spans_both_sides():
    """Шкала общая для сторон: разные максимумы у Radiant и Dire сделали
    бы слабую сторону визуально равной сильной — прямо противоположно
    тому, что карта должна показывать."""
    out = build_heatmaps([
        _cell(team=2, gx=1, n=10),
        _cell(team=3, gx=2, n=40),
    ])
    assert out["phases"]["early"]["presence"]["max_n"] == 40


def test_max_is_the_running_maximum():
    """Максимум обязан переживать клетки, идущие ПОСЛЕ него. Проверка
    мутацией: подмена накопления на присваивание последнего значения не
    роняла ни одного теста, потому что во всех наибольшая клетка была
    последней. Такая ошибка не видна и на картинке — шкала просто
    съезжает, и слабая карта выглядит яркой."""
    out = build_heatmaps([
        _cell(gx=1, n=7), _cell(gx=2, n=99), _cell(gx=3, n=4),
    ])
    assert out["phases"]["early"]["presence"]["max_n"] == 99


def test_empty_combinations_are_absent_not_empty():
    """Пустой список рядом с непустыми неотличим от «не посчитали»."""
    out = build_heatmaps([_cell(phase="early", kind="ward")])
    assert list(out["phases"]) == ["early"]
    assert list(out["phases"]["early"]) == ["ward"]


def test_phase_order_is_chronological():
    out = build_heatmaps([_cell(phase=p) for p in ("late", "early", "mid")])
    assert list(out["phases"]) == ["early", "mid", "late"]


def test_cell_order_is_stable():
    """Отчёт пересобирается при переразборе матча. Плавающий порядок дал
    бы «изменившийся» отчёт там, где не менялось ничего."""
    a = build_heatmaps([_cell(gx=5, gy=1), _cell(gx=1, gy=9), _cell(gx=1, gy=2)])
    b = build_heatmaps([_cell(gx=1, gy=2), _cell(gx=5, gy=1), _cell(gx=1, gy=9)])
    assert a == b
    assert a["phases"]["early"]["presence"]["radiant"] == [
        [1, 2, 5], [1, 9, 5], [5, 1, 5]]


def test_out_of_grid_cells_dropped():
    """Клетка вне сетки — испорченная строка, а не край карты. Нарисовать
    её нельзя, а тихо прижать к границе значило бы соврать о позиции."""
    out = build_heatmaps([_cell(gx=32), _cell(gy=255), _cell(gx=-1)])
    assert out["phases"] == {}


def test_zero_hits_dropped():
    assert build_heatmaps([_cell(n=0)])["phases"] == {}


def test_unknown_phase_or_kind_ignored():
    """Enum в ClickHouse может пополниться раньше кода. Чужое значение
    нельзя молча положить в «presence» — карта станет неверной."""
    out = build_heatmaps([_cell(phase="overtime"), _cell(kind="courier")])
    assert out["phases"] == {}


def test_unknown_team_ignored():
    assert build_heatmaps([_cell(team=0), _cell(team=5)])["phases"] == {}


def test_garbage_row_does_not_crash():
    out = build_heatmaps([{"phase": "early", "kind": "presence",
                           "team": "две", "gx": None, "gy": 1, "n": 1}])
    assert out["phases"] == {}


def test_availability_flag_separates_missing_from_empty():
    """У JSON-матчей (opendota_timeline) координат нет в принципе, у
    реплейных они появляются после разбора. Без флага потребитель не
    отличит это от матча, где никто не ставил вардов, — та же подмена
    «нет данных» на «данные такие», что в спринтах 92 и 99."""
    assert heatmaps_available(build_heatmaps([])) is False
    assert heatmaps_available(build_heatmaps([_cell()])) is True


def test_grid_matches_the_aggregate():
    """32 зашито и в extractor/mapcells.py, и здесь. Разъедутся — карта
    молча съедет в угол."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "feature-extractor" / "src" / "extractor" / "mapcells.py"
           ).read_text(encoding="utf-8")
    assert "GRID = 32" in src
    assert build_heatmaps([_cell()])["grid"] == 32
