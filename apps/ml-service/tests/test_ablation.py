"""Тесты ablation-анализа фич (спринт 69).

Главное, что проверяется, — что инструмент НЕ путает два разных вердикта:
«фича бесполезна» (данные есть, эффекта нет) и «фича неизмерима» (данных
нет вовсе). Смешать их — значит выкинуть по правилу ML-PLAN §6 фичу,
которую просто ещё не на чем было проверить: ровно этот риск создаёт
трек F, где все 12 новых колонок NaN у исторических матчей.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.ablation import (GROUPS, coverage, run, verdict, without)
from training.dataset import FEATURES, synth_matches


def test_without_blanks_only_named_columns():
    ds = synth_matches(40)
    out = without(ds, ["towers_diff"])
    i = FEATURES.index("towers_diff")
    assert np.all(np.isnan(out.X[:, i])), "колонка должна быть полностью NaN"
    others = [j for j in range(len(FEATURES)) if j != i]
    assert np.array_equal(np.isnan(ds.X[:, others]), np.isnan(out.X[:, others])), \
        "остальные колонки не должны меняться"
    # Исходный датасет не тронут (иначе прогон по группам портил бы базу).
    assert not np.all(np.isnan(ds.X[:, i]))


def test_without_preserves_dataset_metadata():
    """n_matches/tiers/patches обязаны пережить копирование: без них
    _valid_mask и patch_weights дадут другой сплит, и сравнение перестанет
    быть парным."""
    ds = synth_matches(30)
    out = without(ds, ["rax_diff"])
    assert out.n_matches == ds.n_matches
    assert np.array_equal(out.y, ds.y) and np.array_equal(out.groups, ds.groups)
    a, b = ds._valid_mask(), out._valid_mask()
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def test_coverage_counts_non_nan():
    ds = synth_matches(20)
    i = FEATURES.index("alive_diff")
    ds.X[:, i] = np.nan
    cov = coverage(ds)
    assert cov["alive_diff"] == 0.0
    assert cov["game_time"] == 1.0


def test_verdict_uses_noise_not_sign():
    # Эффект меньше двух сигм — это шум, каким бы ни был знак.
    assert "шум" in verdict(0.001, 0.003)
    assert "шум" in verdict(-0.001, 0.003)
    # Заметно хуже без фичи — фича полезна.
    assert "ПОЛЕЗНА" in verdict(0.010, 0.003)
    # Заметно лучше без фичи — фича вредит.
    assert "ВРЕДИТ" in verdict(-0.010, 0.003)


def test_statistically_significant_but_tiny_effect_is_not_a_win():
    """Найдено на живом прогоне: парное сравнение двух почти одинаковых
    моделей даёт σ ~0.0001, и микроэффект 0.0003 проходит порог 2σ. Без
    порога практической значимости инструмент объявил бы draft_prior
    полезным — вопреки независимому замеру спринта 62."""
    v = verdict(0.00034, 0.00014)
    assert "ПОЛЕЗНА" not in v
    assert "ничтожен" in v


def test_low_coverage_never_yields_removal_verdict():
    """Тоже с живого прогона: у alive/towers/rax покрытие 5.5% (они есть
    только у реплейных матчей). «Эффекта нет» на 5% датасета — это факт про
    выборку, а не про фичу, и удалять по нему нельзя."""
    v = verdict(0.00015, 0.00009, cov=0.055)
    assert "кандидат на удаление" not in v
    assert "преждевременен" in v
    # При полном покрытии тот же результат уже означает «выкидывать».
    assert "кандидат на удаление" in verdict(0.00001, 0.00050, cov=1.0)


def test_empty_feature_is_unmeasurable_not_useless():
    """Ключевая проверка: у пустой фичи вердикт «неизмерима», и модель
    ради неё не переобучается."""
    ds = synth_matches(60)
    i = FEATURES.index("roshan_diff")
    ds.X[:, i] = np.nan

    rows, base = run(ds, {"F2_объективы": ["roshan_diff"]})
    assert len(rows) == 1
    r = rows[0]
    assert r["delta"] is None, "обучение не должно было запускаться"
    assert "НЕИЗМЕРИМА" in r["verdict"]
    assert "кандидат на удаление" not in r["verdict"]
    assert 0.0 <= base <= 1.0


def test_filled_feature_gets_measured():
    """Обратный случай: если данные есть, вердикт выносится по числам."""
    ds = synth_matches(60)
    i = FEATURES.index("towers_diff")
    rng = np.random.default_rng(0)
    ds.X[:, i] = rng.normal(size=len(ds.y))

    rows, _ = run(ds, {"башни": ["towers_diff"]})
    r = rows[0]
    assert r["delta"] is not None and r["sigma"] is not None
    assert r["coverage"] == 1.0
    assert "НЕИЗМЕРИМА" not in r["verdict"]


def test_groups_reference_only_real_features():
    """Опечатка в GROUPS привела бы к тихому пропуску целого блока."""
    for label, names in GROUPS.items():
        unknown = [n for n in names if n not in FEATURES]
        assert not unknown, f"{label}: нет таких фич — {unknown}"
