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


def test_eval_holdout_prefers_pro_then_valid():
    from training.dataset import PRO_TIER
    import numpy as np

    # без про-матчей → берётся валидационный сплит
    ds = synth_matches(40)
    X, y, groups, kind = ds.eval_holdout()
    assert kind == "valid" and len(y) > 0
    # holdout не пересекается с train по матчам (тот же seed)
    (X_tr, _), _ = ds.split_by_match()
    assert len(X_tr) + len(y) == len(ds.y)

    # если помечаем достаточно матчей как Professional → берётся эталон
    ds2 = synth_matches(40)
    ds2.tiers = np.array([PRO_TIER if g % 2 == 0 else "" for g in ds2.groups])
    X2, y2, g2, kind2 = ds2.eval_holdout(min_bench_matches=5)
    assert kind2 == "benchmark_pro"
    assert set(np.unique(g2).tolist()) == {g for g in set(ds2.groups.tolist()) if g % 2 == 0}


def test_evaluate_gate_fair_head_to_head():
    """Гейт сравнивает обе модели на ОДНОМ holdout: слабая prod-модель,
    обученная на крошечной выборке, не должна блокировать хорошего кандидата."""
    from training.train_winprob import train, evaluate_gate

    big = synth_matches(120, seed=3)
    weak = train(synth_matches(30, seed=99), num_rounds=5, mirror=False)
    strong = train(big, num_rounds=150)
    # оба честно считаются на holdout текущих (больших) данных
    ok, reason = evaluate_gate(strong, weak, big)
    assert ok, reason
    assert "одни данные" in reason
    # обратное: сильную prod не вытесняет заведомо слабый кандидат
    ok2, _ = evaluate_gate(weak, strong, big)
    assert not ok2


def test_evaluate_gate_tie_promotes_newer():
    """В пределах шума (та же модель) кандидат продвигается — предпочитаем
    свежую версию на бОльших данных."""
    from training.train_winprob import train, evaluate_gate

    ds = synth_matches(90, seed=11)
    art = train(ds, num_rounds=100)
    ok, reason = evaluate_gate(art, art, ds)  # идентичные модели → Δ=0
    assert ok and "не хуже" in reason


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

    def fake_push(art, path, log, ds=None):
        pushed.append(art)
        return "v-test", True, "test-promote"

    monkeypatch.setattr(auto, "load_from_clickhouse",
                        lambda *a, **k: synth_matches(holder["n"]))
    monkeypatch.setattr(auto, "push_with_gate", fake_push)
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


def test_psi_zero_on_same_distribution():
    from training.dataset import FEATURES
    from training.drift import compute_reference, max_psi, psi_report

    ds = synth_matches(60, seed=5)
    ref = compute_reference(ds.X, FEATURES)
    report = psi_report(ref, ds.X, FEATURES)
    # те же данные → PSI ≈ 0 по каждой фиче
    assert report and max_psi(report) < 0.01
    # другой seed того же генератора — та же популяция, дрейфа нет
    other = synth_matches(60, seed=6)
    assert max_psi(psi_report(ref, other.X, FEATURES)) < 0.1


def test_psi_detects_shift():
    import numpy as np
    from training.dataset import FEATURES
    from training.drift import compute_reference, psi_report

    ds = synth_matches(60, seed=5)
    ref = compute_reference(ds.X, FEATURES)
    # имитация баланс-патча: экономика раздулась в полтора раза
    shifted = ds.X.copy()
    nw = FEATURES.index("networth_diff")
    shifted[:, nw] = shifted[:, nw] * 1.5 + 4000
    report = psi_report(ref, shifted, FEATURES)
    assert report["networth_diff"] > 0.2          # значимый дрейф пойман
    assert report["game_time"] < 0.05             # нетронутая фича спокойна


def test_psi_constant_feature_no_crash():
    import numpy as np
    from training.drift import compute_reference, max_psi, psi_report

    X = np.column_stack([np.full(100, 7.0), np.random.default_rng(0).normal(size=100)])
    ref = compute_reference(X, ["const", "noise"])
    # константная фича схлопнулась в один бин и в отчёт не попадает
    report = psi_report(ref, X, ["const", "noise"])
    assert "const" not in report and "noise" in report
    assert max_psi(report) < 0.01


def test_autotrain_drift_trigger(monkeypatch, tmp_path):
    """Значимый PSI против production запускает переобучение, даже когда
    новых матчей меньше порога объёма; без изменения витрины дрейф-триггер
    молчит (переобучение на тех же данных дало бы ту же модель)."""
    import numpy as np
    from training import auto
    from training.dataset import FEATURES
    from training.drift import compute_reference

    monkeypatch.setattr(auto, "_last_trained_n", None)
    base = synth_matches(60, seed=5)
    holder = {"ds": base}

    # production обучена на данных ДО «патча»: референс от base
    prod_meta = {"dataset": {"matches": 60},
                 "drift_reference": compute_reference(base.X, FEATURES)}

    class FakeReg:
        def stage_metadata(self, name):
            return prod_meta

    pushed = []
    monkeypatch.setattr(auto, "load_from_clickhouse", lambda *a, **k: holder["ds"])
    monkeypatch.setattr(auto, "push_with_gate",
                        lambda art, path, log, ds=None:
                        (pushed.append(art) or ("v", True, "ok")))
    monkeypatch.setattr(auto, "train",
                        lambda d: {"metrics": {"brier_calibrated": 0.1}})
    monkeypatch.setattr(auto, "registry_from_env", lambda: FakeReg())

    out = tmp_path / "m.pkl"
    assert auto.check_and_train(20, 50, out) == "trained"   # первый прогон
    assert len(pushed) == 1

    # +5 матчей (< 20) без дрейфа — пропуск
    grown = synth_matches(65, seed=5)
    holder["ds"] = grown
    assert auto.check_and_train(20, 50, out) == "not-enough-new"
    assert len(pushed) == 1

    # те же +5 матчей, но экономика уехала (патч) → дрейф-триггер обучает
    drifted = synth_matches(65, seed=5)
    drifted.X = drifted.X.copy()
    nw = FEATURES.index("networth_diff")
    drifted.X[:, nw] = drifted.X[:, nw] * 1.5 + 4000
    holder["ds"] = drifted
    assert auto.check_and_train(20, 50, out) == "trained"
    assert len(pushed) == 2

    # дрейф остался, но витрина не изменилась (65 == последнее обучение) —
    # повторного переобучения на тех же данных нет
    assert auto.check_and_train(20, 50, out) == "not-enough-new"
    assert len(pushed) == 2


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


def test_shap_contributions_sum_to_raw_score():
    """Сумма SHAP-вкладов + bias == сырой скор модели (лог-оддсы):
    sigmoid(суммы) совпадает с некалиброванным предсказанием бустера."""
    import lightgbm as lgb
    import numpy as np
    from explain.winprob_shap import contributions, top_drivers

    ds = synth_matches(80, seed=21)
    art = train(ds, num_rounds=80)
    booster = lgb.Booster(model_str=art["booster"])
    X = ds.X[:50]
    contribs, bias = contributions(booster, X)
    margin = contribs.sum(axis=1) + bias
    proba = 1.0 / (1.0 + np.exp(-margin))
    assert np.allclose(proba, booster.predict(X), atol=1e-6)

    drivers = top_drivers(contribs, FEATURES, k=3)
    assert len(drivers) == len(X) and all(len(d) <= 3 for d in drivers)
    # топ-1 действительно максимален по модулю (вклады округлены до 4 знаков)
    for row, drv in zip(contribs, drivers):
        assert abs(drv[0][1]) >= abs(row).max() - 1e-3
