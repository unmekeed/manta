"""Генератор голден-фикстуры для производных Win Probability (спринт 137).

    make signals-golden-update

Зачем отдельно от голдена коллектора: производные считает libs/wp_rates.py,
и его результат едет в модель по ИМЕНАМ через gRPC из report-generator.
Расхождение здесь проявилось бы не падением, а разными числами у одной и
той же модели в обучении и в проде — то есть ровно так, как ошибку
заметить труднее всего.

test_rates.py рядом проверяет арифметику, но численно — только по
networth_diff: из пятнадцати производных значение было закреплено у
четырёх. Остальные одиннадцать держались на общем цикле, который
проверяет «колонка существует», а не «в колонке правильное число».
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parents[1] / "libs"))

from wp_rates import (RATE_METRICS, RATE_WINDOWS, prev_col,  # noqa: E402
                      prev_time_col, rates_for_row)

FIXTURES = ROOT / "tests" / "fixtures"

# Уровни метрик «сейчас». Специально разного порядка и разных знаков:
# на одинаковых числах перепутанные местами метрики дали бы тот же ряд.
LEVELS = {"networth_diff": 5000.0, "xp_diff": -3200.0, "towers_diff": 2.0,
          "vision_coverage_diff": 0.125, "levels_diff": -4.0}

# Значения тех же метрик в начале каждого окна. Темп по каждой метрике
# должен получиться СВОЙ, поэтому и приросты разные, и знаки разные.
PREV = {
    60:  {"networth_diff": 4100.0, "xp_diff": -2800.0, "towers_diff": 1.0,
          "vision_coverage_diff": 0.100, "levels_diff": -6.0},
    180: {"networth_diff": 2000.0, "xp_diff": -1500.0, "towers_diff": 1.0,
          "vision_coverage_diff": 0.075, "levels_diff": -2.0},
    300: {"networth_diff": -500.0, "xp_diff": 900.0, "towers_diff": 0.0,
          "vision_coverage_diff": 0.000, "levels_diff": 1.0},
}


def _row(game_time: int, spans: dict[int, int]) -> dict:
    """Строка витрины: уровни + оконные колонки.

    `spans` — сколько секунд НАЗАД фактически смотрит каждое окно. Не
    номинал: в начале матча пятиминутного окна ещё нет, и делить надо на
    прошедшее время. Окно, которого нет в spans, в строку не попадает
    вовсе — так выглядит старый кэш и ручной вызов.
    """
    row: dict = {"game_time": game_time}
    row.update(LEVELS)
    for w, span in spans.items():
        row[prev_time_col(w)] = game_time - span
        for m in RATE_METRICS:
            row[prev_col(m, w)] = PREV[w][m]
    return row


def scenarios() -> list[dict]:
    return [
        {
            "name": "окна набраны полностью",
            "why": "середина матча: каждое окно смотрит на свой номинал, "
                   "и три производных по одной метрике обязаны разойтись",
            "row": _row(900, {60: 60, 180: 180, 300: 300}),
        },
        {
            "name": "ранняя игра, истории меньше окна",
            "why": "на второй минуте пятиминутного окна не существует. "
                   "Делить на номинал значило бы занизить темп впятеро "
                   "ровно в той фазе, где Brier худший (0.219 против "
                   "0.103 в поздней), поэтому делим на фактические 60 с — "
                   "и все три окна тут совпадают",
            "row": _row(120, {60: 60, 180: 60, 300: 60}),
        },
        {
            "name": "первая строка матча",
            "why": "окно содержит только саму строку, dt = 0. Ждём NaN, а "
                   "не ноль: ноль означал бы «ничего не менялось», то есть "
                   "измерение там, где его нет",
            "row": _row(0, {60: 0, 180: 0, 300: 0}),
        },
        {
            "name": "строка без оконных колонок",
            "why": "старый кэш витрины и ручной вызов. Все производные — "
                   "NaN, и функция не падает: производная это надстройка, "
                   "ломать существующие пути чтения она не должна",
            "row": _row(600, {}),
        },
        {
            "name": "метрики не менялись",
            "why": "ровный отрезок: темп нулевой по всем пятнадцати. "
                   "Сценарий назван отдельно, а не получается случайно из "
                   "совпавших чисел, — иначе ноль в эталоне нельзя было бы "
                   "отличить от «эту производную забыли посчитать»",
            "row": {"game_time": 900, **LEVELS,
                    **{prev_time_col(w): 900 - w for w in RATE_WINDOWS},
                    **{prev_col(m, w): LEVELS[m]
                       for w in RATE_WINDOWS for m in RATE_METRICS}},
        },
        {
            "name": "дыра в витрине: окно короче номинала",
            "why": "JSON-путь местами дырявый, минуты пропадают. Кадр "
                   "задан в СЕКУНДАХ (RANGE, не ROWS), поэтому дыра "
                   "сокращает фактический пролёт, а не растягивает окно",
            "row": _row(900, {60: 45, 180: 150, 300: 240}),
        },
    ]


def _jsonable(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def main() -> int:
    out = []
    for sc in scenarios():
        rates = rates_for_row(sc["row"], LEVELS)
        if len(rates) != len(RATE_METRICS) * len(RATE_WINDOWS):
            print(f"ОШИБКА: {sc['name']}: {len(rates)} производных",
                  file=sys.stderr)
            return 2
        out.append({**sc,
                    "row": {k: _jsonable(v) for k, v in sc["row"].items()},
                    "expected": {k: _jsonable(v)
                                 for k, v in sorted(rates.items())}})

    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "golden_rates.json").write_text(
        json.dumps({"levels": LEVELS, "scenarios": out},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"эталон производных обновлён: {len(out)} сценариев, "
          f"{len(RATE_METRICS) * len(RATE_WINDOWS)} производных в каждом")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
