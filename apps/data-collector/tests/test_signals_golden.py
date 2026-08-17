"""Регресс ЗНАЧЕНИЙ поминутных фич (спринт 137).

Рядом лежит test_signals.py — 37 тестов, и они проверяют ПОВЕДЕНИЕ:
знак в пользу Radiant, вард живёт шесть минут, аегис пять, пустой лог
даёт ноль. Такие тесты формулируются как свойства и поэтому терпимы к
арифметике: если стоимость выкупа поделить на 12 вместо 13, каждое
свойство останется верным, а числа модели поедут.

Здесь проверяется ровно то, что свойства пропускают: ЧИСЛА. Один
зафиксированный матч на входе, эталонные ряды на выходе, побайтовое
сравнение. Тест не знает игровых правил и не должен: его работа —
заметить, что результат изменился, и заставить человека сказать, хотел
он этого или нет.

Эталон обновляется руками — `make signals-golden-update`. Флага
UPDATE_GOLDEN=1 у теста нет намеренно: возможность переписать эталон
из-под pytest превратила бы регресс в самоподтверждающийся.
"""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collector.signals import all_minute_features  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden() -> tuple[dict, list[int], dict[str, list]]:
    expected = _load("golden_features.json")
    return _load("golden_match.json"), expected["minutes"], expected["features"]


def _same(actual, expected) -> bool:
    """NaN в JSON записан как null; NaN == NaN ложно, поэтому вручную."""
    if expected is None:
        return isinstance(actual, float) and math.isnan(actual)
    if isinstance(actual, float) and math.isnan(actual):
        return False
    # Допуск относительный: unspent_gold_diff делится на 13 и даёт
    # бесконечную дробь, а её двоичное представление зависит от порядка
    # суммирования. Отличие на 1e-9 — не регресс, отличие на копейку уже
    # регресс, и такой допуск ловит второе.
    return math.isclose(float(actual), float(expected), rel_tol=1e-9,
                        abs_tol=1e-9)


def test_feature_set_is_exactly_the_golden_one(golden):
    """Новая фича без эталона — падение, а не молчаливый пропуск.

    Без этой проверки регресс охранял бы только те фичи, которые кто-то
    не забыл внести в фикстуру, и добавление пятнадцатой прошло бы мимо.
    """
    match, minutes, expected = golden
    actual = all_minute_features(match, minutes)
    assert set(actual) == set(expected), (
        f"нет эталона: {sorted(set(actual) - set(expected))}; "
        f"эталон без фичи: {sorted(set(expected) - set(actual))}")


@pytest.mark.parametrize("name", sorted(_load("golden_features.json")["features"]))
def test_feature_values_match_golden(golden, name):
    """Отдельный тест на фичу: в отчёте видно, КАКАЯ фича уехала."""
    match, minutes, expected = golden
    actual = all_minute_features(match, minutes)[name]
    want = expected[name]
    assert len(actual) == len(want)
    bad = [(t, a, w) for t, a, w in zip(minutes, actual, want)
           if not _same(a, w)]
    assert not bad, "\n".join(
        f"  минута {t // 60}: получено {a!r}, эталон {w!r}" for t, a, w in bad)


# -- Тесты на саму фикстуру ----------------------------------------------------
#
# Голден-регресс молча слабеет: достаточно поправить фикстуру так, чтобы
# фича выродилась в ноль, и она останется «проверенной» при любом коде.
# Ниже — проверки, что вход всё ещё задействует то, ради чего заведён.

def test_no_feature_is_degenerate(golden):
    """У каждой фичи в эталоне есть хотя бы два разных значения.

    Ряд из одних нулей сравнивается успешно с любой реализацией, которая
    тоже вернёт нули, — в том числе с `return [0.0] * len(minutes)`.
    """
    _, _, expected = golden
    flat = {name: series[0] for name, series in expected.items()
            if len({json.dumps(v) for v in series}) < 2}
    assert not flat, f"фичи-константы в эталоне: {flat}"


def test_both_signs_are_present_in_the_golden(golden):
    """В эталоне есть и плюс, и минус.

    Знак всех diff-фич завязан на одно место — `_sign`. На входе, где
    Radiant просто везде сильнее, перепутанный знак дал бы зеркальный
    эталон, который так же успешно сравнился бы сам с собой после
    обновления. Смешанные знаки делают ошибку знака заметной глазами.
    """
    _, _, expected = golden
    values = [v for series in expected.values() for v in series
              if isinstance(v, (int, float))]
    assert any(v > 0 for v in values) and any(v < 0 for v in values)


def test_vision_coverage_golden_exercises_overlapping_wards(golden):
    """Площадь обзора считается ОБЪЕДИНЕНИЕМ дисков, а не суммой.

    Это единственное, ради чего фича существует: счётчик obs_wards_diff
    уже отличает «два варда» от «одного», и отличать их второй раз не
    нужно — нужно отличать два разнесённых варда от двух в одном лесу.

    На разнесённых вардах объединение численно РАВНО сумме площадей, и
    подмена одного другим не изменила бы ни одного числа в эталоне. Тест
    требует, чтобы в эталоне была минута с перекрытием: там значение не
    кратно площади одиночного диска.
    """
    from collector.signals import VISION_GRID, _DISC

    _, _, expected = golden
    one_disc = len(_DISC) / float(VISION_GRID * VISION_GRID)
    overlapping = [v for v in expected["vision_coverage_diff"]
                   if v and not math.isclose(
                       (abs(v) / one_disc) % 1.0, 0.0, abs_tol=1e-9)]
    assert overlapping, (
        "в эталоне все значения кратны площади одного варда — перекрытие "
        "не задействовано, и сумма площадей прошла бы вместо объединения")
