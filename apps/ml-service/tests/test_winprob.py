"""Тесты обучающего конвейера Win Probability на синтетике."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.dataset import FEATURES, dataset_hash, merge, synth_matches
from training.train_winprob import train


def test_group_split_no_match_overlap():
    ds = synth_matches(30)
    (X_tr, _), (X_va, _) = ds.split_by_match()
    assert len(X_tr) > 0 and len(X_va) > 0
    assert len(X_tr) + len(X_va) == len(ds.X)


def test_train_produces_calibrated_model():
    ds = synth_matches(120)
    art = train(ds, num_rounds=120)
    # Синтетика по построению предсказуема: калиброванный Brier заметно
    # лучше константного предсказания 0.5 (Brier = 0.25).
    assert art["metrics"]["brier_calibrated"] < 0.2
    assert art["features"] == FEATURES
    assert art["dataset"]["synthetic_matches"] == 120


def test_prediction_monotone_in_networth():
    """Больше преимущество Radiant по золоту → WP не должна падать."""
    import lightgbm as lgb

    art = train(synth_matches(120), num_rounds=120)
    booster = lgb.Booster(model_str=art["booster"])
    cal = art["calibrator"]
    t = 1800.0
    diffs = np.linspace(-30000, 30000, 13)
    X = np.array([[t, d, d * 1.2, d / 3000, 20, d / 30000] for d in diffs])
    wp = cal.predict(booster.predict(X))
    # Допускаем плато (изотоника), но не убывание.
    assert all(b - a >= -1e-9 for a, b in zip(wp, wp[1:]))
    assert wp[0] < 0.35 and wp[-1] > 0.65


def test_dataset_hash_stable_and_merge():
    a, b = synth_matches(5, seed=1), synth_matches(5, seed=2)
    assert dataset_hash(a) == dataset_hash(synth_matches(5, seed=1))
    assert dataset_hash(a) != dataset_hash(b)
    m = merge(a, b)
    assert m.n_matches == 10 and len(m.y) == len(a.y) + len(b.y)


def test_should_promote_gate():
    from training.train_winprob import should_promote

    # Первая версия — всегда promote.
    ok, _ = should_promote({"brier_calibrated": 0.2}, None)
    assert ok
    # Оба с эталоном: решает эталон, валидация игнорируется.
    ok, _ = should_promote(
        {"brier_benchmark_pro": 0.14, "brier_calibrated": 0.30},
        {"brier_benchmark_pro": 0.15, "brier_calibrated": 0.05})
    assert ok
    ok, _ = should_promote(
        {"brier_benchmark_pro": 0.16, "brier_calibrated": 0.01},
        {"brier_benchmark_pro": 0.15, "brier_calibrated": 0.30})
    assert not ok
    # У production нет эталона — новая (оцененная) продвигается.
    ok, reason = should_promote(
        {"brier_benchmark_pro": 0.15, "brier_calibrated": 0.17},
        {"brier_calibrated": 0.08})
    assert ok and "несопоставим" in reason
    # Ни у кого нет эталона — fallback на валидацию.
    ok, _ = should_promote({"brier_calibrated": 0.10},
                           {"brier_calibrated": 0.12})
    assert ok


def test_autotrain_thresholds(monkeypatch, tmp_path):
    """check_and_train: пороги «мало данных» / «мало новых» / «обучаем»."""
    from training import auto

    ds = synth_matches(60)

    class FakeReg:
        def __init__(self, trained_on):
            self.meta = ({"dataset": {"matches": trained_on}}
                         if trained_on is not None else None)

        def stage_metadata(self, name):
            return self.meta

    pushed = []
    monkeypatch.setattr(auto, "load_from_clickhouse",
                        lambda *a, **k: ds)
    monkeypatch.setattr(auto, "push_with_gate",
                        lambda art, path, log: pushed.append(art))
    monkeypatch.setattr(auto, "train",
                        lambda d: {"metrics": {"brier_calibrated": 0.1}})

    out = tmp_path / "m.pkl"
    # Всего матчей меньше минимума.
    monkeypatch.setattr(auto, "registry_from_env", lambda: FakeReg(0))
    assert auto.check_and_train(20, 100, out) == "not-enough-data"
    # Production обучена на 50, новых 10 < 20 — пропуск.
    monkeypatch.setattr(auto, "registry_from_env", lambda: FakeReg(50))
    assert auto.check_and_train(20, 50, out) == "not-enough-new"
    assert not pushed
    # Новых 25 >= 20 — обучаем и пушим.
    monkeypatch.setattr(auto, "registry_from_env", lambda: FakeReg(35))
    assert auto.check_and_train(20, 50, out) == "trained"
    assert len(pushed) == 1
    # Production нет вообще — обучаем при достаточном датасете.
    monkeypatch.setattr(auto, "registry_from_env", lambda: FakeReg(None))
    assert auto.check_and_train(20, 50, out) == "trained"
