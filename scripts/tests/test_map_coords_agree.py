"""Координаты карты в питоне и на фронте обязаны совпадать (спринт 139).

ЗАЧЕМ. Границы карты живут в libs/dota_map.py, но фронт не может его
импортировать, и рядом лежит копия — apps/frontend/src/map/coords.ts.
Копия опасна ровно тем, чем опасно было прежнее тройное определение: она
разойдётся не падением, а КРИВЫМ НАЛОЖЕНИЕМ. Агрегаты посчитаны по одной
сетке, нарисованы по другой; ничего не сломается, просто варды окажутся
не там, где стояли, — и никто не узнает, потому что «примерно похоже».

Спринт 139 и начался с того, что три определения карты разъехались
незаметно. Заводить четвёртое без сторожа — повторять ту же ошибку
осознанно.

ПОЧЕМУ РАЗБОР ТЕКСТОМ, А НЕ ИСПОЛНЕНИЕМ. Запускать node ради трёх чисел
значило бы завести в CI зависимость от собранного фронта. Числа заданы
литералами в обоих файлах, и сравнить литералы достаточно: подмена
литерала выражением сделает тест красным, а это ровно то поведение,
которое здесь нужно.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = ROOT / "libs" / "dota_map.py"
TS = ROOT / "apps" / "frontend" / "src" / "map" / "coords.ts"

# Что сверяем. Только величины, задающие ГЕОМЕТРИЮ: разойдясь, каждая из
# них смещает метки относительно подложки.
SHARED = ("WORLD_HALF", "CELL_MIN", "CELL_MAX")


def py_consts() -> dict[str, float]:
    text = PY.read_text(encoding="utf-8")
    out: dict[str, float] = {}
    # CELL_MIN и CELL_MAX объявлены одной строкой — разбираем оба вида.
    for m in re.finditer(r"^([A-Z_]+)\s*=\s*([0-9.]+)\s*$", text, re.M):
        out[m.group(1)] = float(m.group(2))
    m = re.search(r"^CELL_MIN,\s*CELL_MAX\s*=\s*([0-9]+),\s*([0-9]+)\s*$", text, re.M)
    if m:
        out["CELL_MIN"] = float(m.group(1))
        out["CELL_MAX"] = float(m.group(2))
    return out


def ts_consts() -> dict[str, float]:
    text = TS.read_text(encoding="utf-8")
    return {m.group(1): float(m.group(2)) for m in
            re.finditer(r"^export const ([A-Z_]+)\s*=\s*([0-9.]+)\s*;", text, re.M)}


def test_parsing_found_the_constants():
    """Страховка от теста, который сверяет два пустых словаря.

    Стоит съехать формату объявления — и сравнение ниже начнёт проходить,
    ничего не сравнивая. Зелёный тест вместо отсутствующего хуже, чем
    отсутствующий.
    """
    py, ts = py_consts(), ts_consts()
    for name in SHARED:
        assert name in py, f"{name} не найдена в {PY.name} — разбор сломан"
        assert name in ts, f"{name} не найдена в {TS.name} — разбор сломан"


@pytest.mark.parametrize("name", SHARED)
def test_constants_agree(name):
    py, ts = py_consts(), ts_consts()
    assert py[name] == ts[name], (
        f"{name}: в питоне {py[name]}, на фронте {ts[name]}. Разойдясь, "
        f"они не уронят ничего — просто метки лягут мимо подложки")


def test_units_per_cell_is_derived_on_both_sides():
    """Шаг клетки ВЫВОДИТСЯ из границ в обоих файлах, а не записан числом.

    Записанный литералом, он пережил бы правку WORLD_HALF и увёл бы
    системы врозь молча — а этот тест сверяет только литералы и такого
    расхождения не увидел бы.
    """
    py_text = PY.read_text(encoding="utf-8")
    ts_text = TS.read_text(encoding="utf-8")
    assert re.search(r"UNITS_PER_CELL\s*=\s*\(?2", py_text), \
        "в питоне UNITS_PER_CELL перестал выводиться из WORLD_HALF"
    assert re.search(r"UNITS_PER_CELL\s*=\s*\(2 \* WORLD_HALF\)", ts_text), \
        "на фронте UNITS_PER_CELL перестал выводиться из WORLD_HALF"
