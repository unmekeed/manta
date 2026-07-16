"""Тесты PSI-дрейфа (training.drift): стабильность, сдвиг, эталон в артефакте."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.dataset import FEATURES, synth_matches
from training.drift import feature_psi, psi, reference_hist
from training.train_winprob import train


def test_psi_zero_on_identical_distribution():
    e = np.array([0.1] * 10)
    assert psi(e, e) == 0.0


def test_psi_large_on_shift():
    e = np.array([0.5, 0.5, 0.0, 0.0])
    a = np.array([0.0, 0.0, 0.5, 0.5])
    assert psi(e, a) > 1.0


def test_feature_psi_stable_vs_shifted():
    rng = np.random.default_rng(7)
    X_ref = rng.normal(size=(5000, len(FEATURES)))
    ref = reference_hist(X_ref)

    # Та же генеральная совокупность — PSI мал по всем фичам.
    stable = feature_psi(ref, rng.normal(size=(5000, len(FEATURES))))
    assert set(stable) == set(FEATURES)
    assert max(stable.values()) < 0.05

    # Сдвиг networth_diff на 2σ («мета уехала») — PSI только у неё большой.
    X_shift = rng.normal(size=(5000, len(FEATURES)))
    X_shift[:, FEATURES.index("networth_diff")] += 2.0
    shifted = feature_psi(ref, X_shift)
    assert shifted["networth_diff"] > 0.25
    assert shifted["game_time"] < 0.05


def test_artifact_carries_feature_reference():
    ds = synth_matches(60)
    art = train(ds, num_rounds=30)
    ref = art["feature_reference"]
    assert set(ref) == set(FEATURES)
    # Эталон согласован сам с собой: PSI данных обучения против него ~0.
    scores = feature_psi(ref, ds.X)
    assert max(scores.values()) < 0.02


def test_feature_psi_skips_unknown_feature():
    X = np.zeros((100, len(FEATURES)))
    ref = reference_hist(X + 1.0)
    del ref["position_advance"]  # prod старее текущего набора фич
    scores = feature_psi(ref, X)
    assert "position_advance" not in scores
