"""Тесты даунвейта старого патча (A9, Dataset.patch_weights)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.dataset import Dataset


def _ds(patches):
    n = len(patches)
    return Dataset(X=np.zeros((n, 1)), y=np.zeros(n, dtype=np.int64),
                   groups=np.arange(n, dtype=np.int64), n_matches=n,
                   patches=np.array(patches, dtype=np.int64))


def test_latest_and_unknown_full_weight(monkeypatch):
    monkeypatch.setenv("PATCH_OLD_WEIGHT", "0.4")
    w = _ds([60, 60, 0]).patch_weights()
    assert list(w) == [1.0, 1.0, 1.0]   # текущий патч и неизвестный — без штрафа


def test_old_patch_downweighted_geometrically(monkeypatch):
    monkeypatch.setenv("PATCH_OLD_WEIGHT", "0.4")
    w = _ds([60, 59, 58, 0]).patch_weights()
    assert w[0] == 1.0
    assert abs(w[1] - 0.4) < 1e-9       # на патч старше — ×0.4
    assert abs(w[2] - 0.16) < 1e-9      # на два — ×0.16
    assert w[3] == 1.0


def test_clamped_from_below(monkeypatch):
    monkeypatch.setenv("PATCH_OLD_WEIGHT", "0.4")
    w = _ds([60, 50]).patch_weights()
    assert w[1] == 0.1                  # очень старый — пол 0.1, не ноль


def test_no_patches_all_ones():
    ds = Dataset(X=np.zeros((3, 1)), y=np.zeros(3, dtype=np.int64),
                 groups=np.arange(3, dtype=np.int64), n_matches=3)
    assert list(ds.patch_weights()) == [1.0, 1.0, 1.0]
    assert list(_ds([0, 0]).patch_weights()) == [1.0, 1.0]
