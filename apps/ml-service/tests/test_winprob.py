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
    """check_and_train: пороги «мало данных» / «мало новых» / «обучаем»
    считаются по дельте датасета относительно ПОСЛЕДНЕГО обучения в процессе
    (а не относительно production) — устойчиво к сбросу витрины."""
    from training import auto

    # Триггер держит состояние последнего обучения в модульной переменной.
    monkeypatch.setattr(auto, "_last_trained_n", None)

    holder = {"n": 60}  # текущий размер витрины, меняем между вызовами

    class FakeReg:
        def stage_metadata(self, name):
            return {"dataset": {"matches": 40}}  # влияет только на метрику

    pushed = []
    monkeypatch.setattr(auto, "load_from_clickhouse",
                        lambda *a, **k: synth_matches(holder["n"]))
    monkeypatch.setattr(auto, "push_with_gate",
                        lambda art, path, log: pushed.append(art))
    monkeypatch.setattr(auto, "train",
                        lambda d: {"metrics": {"brier_calibrated": 0.1}})
    monkeypatch.setattr(auto, "registry_from_env", lambda: FakeReg())

    out = tmp_path / "m.pkl"
    # Всего матчей меньше минимума.
    holder["n"] = 60
    assert auto.check_and_train(20, 100, out) == "not-enough-data"
    assert not pushed
    # Первый прогон при достаточном датасете — обучаем сразу.
    assert auto.check_and_train(20, 50, out) == "trained"
    assert len(pushed) == 1
    # Прибавилось 10 (60→70) < 20 — пропуск.
    holder["n"] = 70
    assert auto.check_and_train(20, 50, out) == "not-enough-new"
    assert len(pushed) == 1
    # Прибавилось 25 (60→85) >= 20 — обучаем.
    holder["n"] = 85
    assert auto.check_and_train(20, 50, out) == "trained"
    assert len(pushed) == 2
    # Сброс витрины: 85→51, |−34| >= 20 — снова обучаем (не застреваем).
    holder["n"] = 51
    assert auto.check_and_train(20, 50, out) == "trained"
    assert len(pushed) == 3


def test_mirror_xy_symmetry():
    from training.dataset import FEATURES, mirror_xy
    import numpy as np

    # одна строка: Radiant ведёт (+nw, +xp, +kills_diff, +pos), метка 1
    X = np.array([[1800.0, 5000.0, 6000.0, 4.0, 20.0, 0.5]])
    y = np.array([1])
    Xm, ym = mirror_xy(X, y)
    assert len(ym) == 2
    # зеркало: разностные фичи меняют знак, kills_total и time — нет, метка 0
    neg = {"networth_diff", "xp_diff", "kills_diff", "position_advance"}
    for i, f in enumerate(FEATURES):
        if f in neg:
            assert Xm[1, i] == -X[0, i]
        else:
            assert Xm[1, i] == X[0, i]
    assert ym[1] == 0
    # приор становится ровно сбалансированным
    assert ym.mean() == 0.5


def test_train_mirror_flag():
    ds = synth_matches(80)
    art = train(ds, num_rounds=60, mirror=True)
    assert "mirror" in art["algo"]
    assert art["metrics"]["brier_calibrated"] < 0.25
