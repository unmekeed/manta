"""Тесты производных Win Probability — трек G, пункт G1 (спринт 131).

До этого спринта все 27 фич модели были УРОВНЯМИ. LightGBM смотрит на
поминутную строку независимо и не может увидеть, растёт разрыв или
схлопывается: он знает, что Radiant впереди на 5000, и не знает, что
минуту назад было 8000.

Здесь проверяется арифметика производной и её согласованность с
остальной моделью. Поведение самих оконных функций ClickHouse (окно
строго назад, кадр в секундах, разделение по матчам) проверяется в
test_rates_sql.py на живом сервере — строковыми тестами это не ловится.
"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wp_rates import (RATE_FEATURES, RATE_METRICS, RATE_WINDOWS,  # noqa: E402
                      prev_col, prev_time_col, rate_name)

from training.dataset import (FEATURES, MIRROR_NEGATE,  # noqa: E402
                              align_to_artifact, match_rows_sql, mirror_xy,
                              row_to_features)


def _idx(metric: str, window_s: int) -> int:
    return FEATURES.index(rate_name(metric, window_s))


def _row(game_time: int = 600, **kw) -> dict:
    base = {"game_time": game_time, "networth_diff": 5000, "xp_diff": 6000,
            "kills_radiant": 5, "kills_dire": 3}
    base.update(kw)
    return base


# -- арифметика производной ------------------------------------------------------

def test_rate_is_change_per_minute():
    """Минуту назад было 3000, сейчас 5000 — это +2000 золота в минуту."""
    row = _row(600, networth_diff=5000,
               **{prev_time_col(60): 540, prev_col("networth_diff", 60): 3000})
    assert row_to_features(row)[_idx("networth_diff", 60)] == 2000.0


def test_rate_divides_by_actual_span_not_by_nominal_window():
    """Делить надо на ФАКТИЧЕСКИ прошедшее время.

    В начале матча пятиминутного окна ещё не существует: на второй минуте
    истории всего минута. Деление на номинал (5) занизило бы темп впятеро
    ровно в той фазе, ради которой производные и вводятся — ранний Brier
    0.219 против 0.103 в поздней игре.
    """
    row = _row(120, networth_diff=1000,
               **{prev_time_col(300): 60, prev_col("networth_diff", 300): 0})
    assert row_to_features(row)[_idx("networth_diff", 300)] == 1000.0


def test_rate_over_three_minutes_is_averaged_over_three_minutes():
    """Окно шире одной минуты — это средний темп, а не суммарная дельта."""
    row = _row(420, networth_diff=4000,
               **{prev_time_col(180): 240, prev_col("networth_diff", 180): 3000})
    got = row_to_features(row)[_idx("networth_diff", 180)]
    assert abs(got - 1000.0 / 3.0) < 1e-9, got


def test_first_row_of_a_match_has_no_rate():
    """У первой строки истории нет: окно содержит только её саму.

    Обязателен NaN, а не ноль. Ноль означал бы «ничего не менялось» —
    осмысленное измерение там, где измерения не существует; LightGBM
    учился бы на нём как на факте.
    """
    row = _row(60, networth_diff=0,
               **{prev_time_col(60): 60, prev_col("networth_diff", 60): 0})
    v = row_to_features(row)
    assert math.isnan(v[_idx("networth_diff", 60)])


def test_unknown_metric_gives_unknown_rate():
    """vision_coverage_diff есть только у реплейных матчей: нет уровня —
    нет и производной, и подставлять сюда ноль нельзя."""
    row = _row(600, vision_coverage_diff=None,
               **{prev_time_col(60): 540,
                  prev_col("vision_coverage_diff", 60): None})
    assert math.isnan(row_to_features(row)[_idx("vision_coverage_diff", 60)])


def test_rows_without_window_columns_still_work():
    """Строка, собранная не через match_rows_sql (ручные вызовы, старый
    кэш), обязана давать NaN, а не падать: производная — надстройка, она
    не должна ломать существующие пути чтения витрины."""
    v = row_to_features(_row(600))
    assert all(math.isnan(v[FEATURES.index(f)]) for f in RATE_FEATURES)


def test_negative_rate_when_advantage_collapses():
    """Главный смысл фичи: отличить «ведёт и разгоняется» от «ведёт, но
    теряет» — состояние в обоих случаях одинаковое."""
    row = _row(600, networth_diff=5000,
               **{prev_time_col(60): 540, prev_col("networth_diff", 60): 8000})
    assert row_to_features(row)[_idx("networth_diff", 60)] == -3000.0


# -- согласованность с остальной моделью ------------------------------------------

def test_every_rate_is_mirrored():
    """Производная разностной величины меняет знак вместе с ней.

    Забытое зеркалирование НЕ падает: модель просто учится на данных, где
    состояние отражено, а его производная — нет. Такое находится только
    тестом.
    """
    for m in RATE_METRICS:
        for w in RATE_WINDOWS:
            name = rate_name(m, w)
            assert (name in MIRROR_NEGATE) == (m in MIRROR_NEGATE), (
                f"{name} и {m} обязаны зеркалиться одинаково")


def test_mirror_negates_rate_values():
    X = np.full((1, len(FEATURES)), np.nan)
    i = _idx("networth_diff", 60)
    X[0, i] = 1500.0
    Xm, ym = mirror_xy(X, np.array([1]))
    assert Xm[1, i] == -1500.0
    assert ym[1] == 0


def test_monotone_covers_every_feature():
    """tune.py собирает ограничения как [MONOTONE[f] for f in FEATURES] —
    забытая фича роняет подбор гиперпараметров по KeyError, причём не при
    добавлении фичи, а через недели, при следующем запуске tune."""
    from training.train_winprob import MONOTONE

    assert set(MONOTONE) == set(FEATURES)


def test_rates_are_unconstrained_for_now():
    """Спринт вводит производные, чтобы ПРОВЕРИТЬ, есть ли в них сигнал.
    Монотонное ограничение — доменное утверждение, заданное заранее;
    окажись оно неверным, абляция покажет «бесполезно», и мы не отличим
    неработающую идею от неверной гипотезы, которой связали модель."""
    from training.train_winprob import MONOTONE

    assert all(MONOTONE[f] == 0 for f in RATE_FEATURES)


def test_rate_families_are_ablated_separately():
    """Скорее всего работает одно окно из трёх. Мерить их скопом значило
    бы утопить сработавшее семейство в двух несработавших."""
    from training.ablation import GROUPS

    for w in RATE_WINDOWS:
        assert GROUPS[f"G1_окно_{w // 60}мин"] == [
            rate_name(m, w) for m in RATE_METRICS]
    assert GROUPS["G1_производные_все"] == list(RATE_FEATURES)


# -- SQL: структурная защита от утечки --------------------------------------------

def test_sql_windows_look_only_backwards():
    """Окно ТОЛЬКО назад.

    Заглядывание вперёд — утечка целевой переменной: даёт красивый Brier
    на валидации и мусор в проде, потому что в реальном матче будущего
    ещё нет. Проверка структурная и потому грубая, но утечка — ровно тот
    случай, где грубая защита оправдана: поведенческий тест на это есть
    только в test_rates_sql.py, а он требует живого ClickHouse.
    """
    sql = match_rows_sql()
    assert "FOLLOWING" not in sql.upper()
    for w in RATE_WINDOWS:
        assert f"RANGE BETWEEN {w} PRECEDING AND CURRENT ROW" in sql


def test_sql_asks_for_every_window_column_the_code_reads():
    """Имена колонок в SQL и в row_to_features обязаны совпадать: иначе
    производные молча станут NaN на всём датасете, а метрика — «фича не
    работает» вместо «фича не доехала»."""
    sql = match_rows_sql()
    for w in RATE_WINDOWS:
        assert f"AS {prev_time_col(w)}" in sql
        for m in RATE_METRICS:
            assert f"AS {prev_col(m, w)}" in sql


# -- колонки под артефакт ---------------------------------------------------------

def test_align_keeps_columns_of_an_older_artifact():
    """Артефакт, обученный до G1, знает 27 фич — и обязан продолжать
    работать на новом коде, пока новый не прошёл гейт."""
    X = np.arange(len(FEATURES), dtype=float).reshape(1, -1)
    old = FEATURES[:27]
    out = align_to_artifact(X, old)
    assert out.shape == (1, 27)
    assert list(out[0]) == list(range(27))


def test_align_follows_names_not_positions():
    """Ровно то, ради чего позиционная обрезка заменена.

    Трек G предполагает УДАЛЕНИЕ не оправдавших себя фич. Удаление из
    середины FEATURES обрезку не ломает — оно молча сдвигает колонки, и
    модель получает networth там, где ждала xp. Это не падает и не видно
    в метриках инференса.
    """
    X = np.arange(len(FEATURES), dtype=float).reshape(1, -1)
    out = align_to_artifact(X, ["xp_diff", "networth_diff"])
    assert list(out[0]) == [float(FEATURES.index("xp_diff")),
                            float(FEATURES.index("networth_diff"))]


def test_align_gives_nan_for_a_feature_that_no_longer_exists():
    """Фича, удалённая из FEATURES, но оставшаяся в старом артефакте —
    NaN (нативный пропуск LightGBM), а не падение: прод не должен
    вставать из-за того, что мы почистили список фич."""
    X = np.zeros((2, len(FEATURES)))
    out = align_to_artifact(X, ["networth_diff", "давно_удалённая"])
    assert not np.isnan(out[:, 0]).any()
    assert np.isnan(out[:, 1]).all()
