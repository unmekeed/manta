"""Регресс ЗНАЧЕНИЙ вектора фич модели (спринт 137).

Строка витрины на входе, весь вектор в порядке FEATURES на выходе.

Два свойства, которых не проверял никто:

  ПОРЯДОК. Перестановка двух колонок ничего не ломает заметно — модель
  просто молча учится на перепутанных признаках. Расхождение FEATURES и
  сборки вектора уже стоило проекту нескольких суток простоя (спринты 90
  и 91), после чего сборку перевели на имена, но численно порядок так и
  остался незакреплённым.

  ПОЛИТИКА ПРОПУСКОВ. Отсутствующая фича обязана быть NaN, а не нулём:
  ноль у разностной фичи означает «ровно посередине» — то есть ложный
  сигнал вместо честного пропуска, который LightGBM умеет обрабатывать
  сам. Правило живёт в одной функции `_f`, и подмена её на `or 0.0` не
  ломает ни один тип и не падает ни на одном ассерте про поведение.

Эталон обновляется руками — `make signals-golden-update`.
"""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parents[1] / "libs"))

from training.dataset import FEATURES, row_to_features  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "golden_vector.json").read_text(encoding="utf-8"))


def _same(actual, expected) -> bool:
    if expected is None:
        return isinstance(actual, float) and math.isnan(actual)
    if isinstance(actual, float) and math.isnan(actual):
        return False
    return math.isclose(float(actual), float(expected), rel_tol=1e-12,
                        abs_tol=1e-12)


def test_feature_list_matches_the_golden_order():
    """Сам список FEATURES и его ПОРЯДОК закреплены.

    Добавление фичи в конец — законная правка, и она обязана пройти через
    обновление эталона. Перестановка существующих — почти всегда ошибка,
    и без этой проверки она проходит молча.
    """
    assert list(FEATURES) == GOLDEN["features"]


@pytest.mark.parametrize("sc", GOLDEN["scenarios"], ids=lambda s: s["name"])
def test_vector_matches_golden(sc):
    actual = row_to_features(sc["row"])
    assert len(actual) == len(GOLDEN["features"])
    bad = [(f, a, w) for f, a, w in zip(GOLDEN["features"], actual,
                                        sc["expected"]) if not _same(a, w)]
    assert not bad, f"{sc['why']}\n" + "\n".join(
        f"  {f}: получено {a!r}, эталон {w!r}" for f, a, w in bad)


@pytest.mark.parametrize("sc", GOLDEN["scenarios"], ids=lambda s: s["name"])
def test_absent_features_are_nan_never_zero(sc):
    """Каждый None во входной строке даёт NaN на выходе, а не ноль.

    Проверка идёт не по эталону, а по самой строке: даже если эталон
    когда-нибудь обновят с нулями, этот тест упадёт.
    """
    actual = dict(zip(GOLDEN["features"], row_to_features(sc["row"])))
    for name, value in sc["row"].items():
        if value is None and name in actual:
            assert math.isnan(actual[name]), (
                f"{name} отсутствует в строке, но пришёл как {actual[name]!r}; "
                "ноль у разностной фичи — ложный сигнал «ровно посередине»")


# -- Тесты на саму фикстуру ----------------------------------------------------

def test_golden_covers_every_model_feature():
    """Эталон покрывает ВСЕ фичи модели, а не подмножество."""
    assert len(GOLDEN["features"]) == len(FEATURES)
    for sc in GOLDEN["scenarios"]:
        assert len(sc["expected"]) == len(FEATURES), sc["name"]


def test_golden_distinguishes_every_pair_of_features():
    """Никакие две фичи не совпадают во ВСЕХ сценариях.

    Это и есть работающая проверка порядка. Сравнение вектора поймает
    перестановку только если у переставленных колонок разные числа: две
    фичи, у которых во всех семи сценариях одинаковые значения, можно
    поменять местами, и эталон этого не заметит.
    """
    profiles: dict[tuple, list[str]] = {}
    for i, name in enumerate(GOLDEN["features"]):
        key = tuple(json.dumps(sc["expected"][i]) for sc in GOLDEN["scenarios"])
        profiles.setdefault(key, []).append(name)
    collisions = [v for v in profiles.values() if len(v) > 1]
    assert not collisions, (
        f"неразличимые в эталоне фичи: {collisions} — их перестановка "
        "прошла бы сравнение вектора")


def test_golden_exercises_both_present_and_absent(golden_names=None):
    """Есть сценарий без пропусков и сценарий с пропусками.

    На одних полных строках политика NaN не проверяется вовсе; на одних
    дырявых — не проверяется арифметика.
    """
    nan_counts = [sum(1 for v in sc["expected"] if v is None)
                  for sc in GOLDEN["scenarios"]]
    assert 0 in nan_counts, "нет сценария, где известны все фичи"
    assert any(n > 0 for n in nan_counts), "нет сценария с пропусками"
