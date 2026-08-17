"""Регресс ЗНАЧЕНИЙ производных Win Probability (спринт 137).

test_rates.py рядом проверяет арифметику как свойства — «темп это
изменение за минуту», «делим на фактический пролёт», «dt=0 даёт NaN».
Численно там закреплён только networth_diff: из пятнадцати производных
значение имели четыре, остальные держались на цикле, который проверяет
существование колонки, а не число в ней.

Здесь закреплены все пятнадцать на пяти сценариях. Эталон обновляется
руками — `make signals-golden-update`.
"""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parents[1] / "libs"))

from wp_rates import (RATE_FEATURES, RATE_METRICS,  # noqa: E402
                      RATE_WINDOWS, rates_for_row)

# Имена производных выписаны буквами, а не собраны из RATE_METRICS и
# RATE_WINDOWS. Собранный список сверялся бы сам с собой: смена окна с 180
# на 120 или правила имён с `_rate_3m` на `_rate_180s` прошла бы молча.
#
# А молча она пройти не должна: report-generator шлёт вектор фич в
# ml-service ПО ИМЕНАМ через gRPC, и переименование колонки в одном месте
# проявилось бы не падением, а разными числами у одной и той же модели в
# обучении и в проде.
EXPECTED_RATE_FEATURES = [
    "networth_diff_rate_1m", "xp_diff_rate_1m", "towers_diff_rate_1m",
    "vision_coverage_diff_rate_1m", "levels_diff_rate_1m",
    "networth_diff_rate_3m", "xp_diff_rate_3m", "towers_diff_rate_3m",
    "vision_coverage_diff_rate_3m", "levels_diff_rate_3m",
    "networth_diff_rate_5m", "xp_diff_rate_5m", "towers_diff_rate_5m",
    "vision_coverage_diff_rate_5m", "levels_diff_rate_5m",
]


def test_rate_feature_names_are_frozen():
    """Список имён производных и его порядок закреплены буквально."""
    assert RATE_FEATURES == EXPECTED_RATE_FEATURES

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "golden_rates.json").read_text(encoding="utf-8"))


def _same(actual, expected) -> bool:
    """null в фикстуре — это NaN; NaN == NaN ложно, поэтому вручную."""
    if expected is None:
        return isinstance(actual, float) and math.isnan(actual)
    if isinstance(actual, float) and math.isnan(actual):
        return False
    return math.isclose(float(actual), float(expected), rel_tol=1e-12,
                        abs_tol=1e-12)


@pytest.mark.parametrize("sc", GOLDEN["scenarios"], ids=lambda s: s["name"])
def test_rates_match_golden(sc):
    actual = rates_for_row(sc["row"], GOLDEN["levels"])
    assert set(actual) == set(sc["expected"]), (
        f"нет эталона: {sorted(set(actual) - set(sc['expected']))}; "
        f"эталон без производной: {sorted(set(sc['expected']) - set(actual))}")
    bad = [(k, actual[k], v) for k, v in sc["expected"].items()
           if not _same(actual[k], v)]
    assert not bad, f"{sc['why']}\n" + "\n".join(
        f"  {k}: получено {a!r}, эталон {w!r}" for k, a, w in bad)


# -- Тесты на саму фикстуру ----------------------------------------------------

def test_golden_covers_every_rate_feature():
    """Все пятнадцать производных закреплены, а не подмножество."""
    want = len(RATE_METRICS) * len(RATE_WINDOWS)
    for sc in GOLDEN["scenarios"]:
        assert len(sc["expected"]) == want, sc["name"]


def test_every_rate_feature_has_a_nonzero_value_somewhere():
    """У каждой производной есть сценарий с ненулевым числом.

    Производная, у которой во всех сценариях ноль или NaN, закреплена
    только на бумаге: `return 0.0` прошёл бы её эталон целиком.
    """
    alive = {k for sc in GOLDEN["scenarios"] for k, v in sc["expected"].items()
             if isinstance(v, (int, float)) and v}
    missing = sorted({k for sc in GOLDEN["scenarios"] for k in sc["expected"]}
                     - alive)
    assert not missing, f"всюду ноль или NaN: {missing}"


def test_golden_distinguishes_windows_and_metrics():
    """Пятнадцать производных дают пятнадцать РАЗНЫХ профилей значений.

    Если два столбца совпали во всех сценариях, эталон не отличит их друг
    от друга: перепутанные местами окна (1m вместо 3m) или метрики прошли
    бы сравнение. Профиль — это ряд значений фичи по сценариям.
    """
    profiles: dict[tuple, list[str]] = {}
    names = sorted(GOLDEN["scenarios"][0]["expected"])
    for name in names:
        key = tuple(json.dumps(sc["expected"][name])
                    for sc in GOLDEN["scenarios"])
        profiles.setdefault(key, []).append(name)
    collisions = [v for v in profiles.values() if len(v) > 1]
    assert not collisions, f"неразличимые в эталоне производные: {collisions}"
