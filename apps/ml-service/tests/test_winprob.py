"""Тесты обучающего конвейера Win Probability на синтетике."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.dataset import FEATURES, dataset_hash, merge, synth_matches
from training.train_winprob import train


def _pad(row: list[float]) -> list[float]:
    """Дополнить вектор до текущего числа фич NaN'ами: тесты перечисляют
    только базовые 10, остальные (треки F и G) для них не заданы.

    Хелпер жил ВНУТРИ test_autotrain_thresholds — скриптовая правка
    спринтов 60–66 вклинила его между сигнатурой теста и телом. Тело
    оказалось недостижимым кодом после `return` внутри `_pad`, а сам тест
    сжался до двух строк: докстринг и импорт. Он проходил и не проверял
    ничего — а сторожил он пороги АВТОМАТИЧЕСКОГО переобучения.
    """
    return row + [float("nan")] * (len(FEATURES) - len(row))


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
    # Фичи трека F не задаём — NaN, как у матчей, собранных до них.
    X = np.array([_pad([t, d, d * 1.2, d / 3000, 20, d / 30000,
                        d / 10000, d / 6000, d / 15000, d / 40000])
                  for d in diffs])
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

    # если помечаем достаточно матчей как Professional → берётся эталон.
    # С 2026-07-31 эталон — ПОДМНОЖЕСТВО про-матчей: половина уходит в
    # обучение (PRO_TRAIN_FRAC), иначе модель не видит про-домен вовсе.
    ds2 = synth_matches(40)
    ds2.tiers = np.array([PRO_TIER if g % 2 == 0 else "" for g in ds2.groups])
    X2, y2, g2, kind2 = ds2.eval_holdout(min_bench_matches=5)
    assert kind2 == "benchmark_pro"
    all_pro = {g for g in set(ds2.groups.tolist()) if g % 2 == 0}
    bench = set(np.unique(g2).tolist())
    assert bench and bench < all_pro          # непустое строгое подмножество
    # и ни один эталонный матч не попал в обучение
    in_valid, pro = ds2._valid_mask()
    assert bench & set(ds2.groups[~in_valid & ~pro].tolist()) == set()


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


def test_autotrain_replaces_legacy_production_without_digest(monkeypatch,
                                                              tmp_path):
    """Недоверенный легаси-артефакт не загружается, но не должен навсегда
    блокировать обучение его доверенной замены."""
    from registry import ArtifactIntegrityError
    from training import auto

    monkeypatch.setattr(auto, "_last_trained_n", None)

    class LegacyReg:
        def stage_metadata(self, name):
            raise ArtifactIntegrityError("metadata has no artifact_sha256")

    pushed = []
    monkeypatch.setattr(auto, "load_from_clickhouse",
                        lambda *a, **k: synth_matches(60))
    monkeypatch.setattr(auto, "registry_from_env", lambda: LegacyReg())
    monkeypatch.setattr(auto, "train",
                        lambda d: {"metrics": {"brier_calibrated": 0.1}})
    monkeypatch.setattr(auto, "push_with_gate",
                        lambda art, path, log, ds=None:
                        (pushed.append(art) or ("v", True, "первая версия")))

    assert auto.check_and_train(20, 50, tmp_path / "model.pkl") == "trained"
    assert len(pushed) == 1


def test_gate_promotes_trusted_replacement_for_legacy_production(monkeypatch,
                                                                 tmp_path):
    """После push гейт тоже встречает старый production. Он не должен
    загружать его для сравнения, но должен продвинуть новый digest-артефакт."""
    import logging
    import registry
    from registry import ArtifactIntegrityError
    from training.train_winprob import push_with_gate

    promoted = []

    class LegacyReg:
        def push(self, name, artifact, metadata):
            return "trusted-v2"

        def resolve(self, name, ref):
            raise ArtifactIntegrityError("metadata has no artifact_sha256")

        def promote(self, name, version):
            promoted.append((name, version))

    monkeypatch.setattr(registry, "registry_from_env", lambda: LegacyReg())
    model = tmp_path / "model.pkl"
    model.write_bytes(b"new trusted artifact")
    artifact = {
        "model_version": "1.0.0",
        "algo": "test",
        "features": [],
        "metrics": {},
        "dataset": {},
        "trained_at": "now",
    }

    version, ok, reason = push_with_gate(
        artifact, model, logging.getLogger("test"), ds=object())
    assert (version, ok, reason) == ("trusted-v2", True, "первая версия")
    assert promoted == [("win_probability", "trusted-v2")]


def test_psi_zero_on_same_distribution():
    from training.dataset import FEATURES
    from training.drift import compute_reference, max_psi, psi_report

    # networth_rel почти детерминирована драфтом матча → распределение по
    # матчам шумное; для проверки «нет дрейфа между сэмплами одной
    # популяции» нужна выборка побольше
    ds = synth_matches(250, seed=5)
    ref = compute_reference(ds.X, FEATURES)
    report = psi_report(ref, ds.X, FEATURES)
    # те же данные → PSI ≈ 0 по каждой фиче
    assert report and max_psi(report) < 0.01
    # другой seed того же генератора — та же популяция, дрейфа нет
    other = synth_matches(250, seed=6)
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

    # одна строка: Radiant ведёт (+nw, +xp, +kills_diff, +pos, +alive,
    # +towers, +rax), метка 1
    X = np.array([_pad([1800.0, 5000.0, 6000.0, 4.0, 20.0, 0.5, 2.0, 3.0,
                        1.0, 0.12])])
    y = np.array([1])
    Xm, ym = mirror_xy(X, y)
    assert len(ym) == 2
    # Зеркало: разностные фичи меняют знак; draft_prior — вероятность,
    # поэтому переходит в 1−p; game_time/kills_total симметричны. Метка
    # инвертируется. Незаданные фичи остаются NaN (NaN != NaN, поэтому
    # сравниваем через isnan).
    from training.dataset import MIRROR_COMPLEMENT, MIRROR_NEGATE
    for i, f in enumerate(FEATURES):
        src, got = X[0, i], Xm[1, i]
        if np.isnan(src):
            assert np.isnan(got), f"{f}: NaN должен остаться NaN"
        elif f in MIRROR_NEGATE:
            assert got == -src, f"{f} обязана менять знак"
        elif f in MIRROR_COMPLEMENT:
            assert got == 1.0 - src, f"{f} обязана переходить в 1−p"
        else:
            assert got == src, f"{f} обязана остаться прежней"
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


def test_row_to_features_missing_position_is_nan():
    """Отсутствие position_advance (JSON-матчи) = NaN, не 0: ноль — это
    «бой в центре карты», ложный сигнал."""
    import math
    from training.dataset import row_to_features

    base = {"game_time": 600, "networth_diff": 1000, "xp_diff": 1200,
            "kills_radiant": 5, "kills_dire": 3}
    # null из ClickHouse → None
    assert math.isnan(row_to_features({**base, "position_advance": None})[5])
    # ключа нет вовсе (старые данные)
    assert math.isnan(row_to_features(base)[5])
    # реальное значение проходит как есть
    assert row_to_features({**base, "position_advance": 0.4})[5] == 0.4


def test_psi_ignores_nan_rows():
    """PSI сравнивает распределения наблюдаемых значений: рост доли
    JSON-матчей (NaN в position_advance) — не дрейф самой фичи."""
    import numpy as np
    from training.drift import compute_reference, psi_report

    rng = np.random.default_rng(3)
    ref_X = rng.normal(0, 1, size=(4000, 1))
    ref = compute_reference(ref_X, ["pos"])
    # то же распределение, но 80% строк — NaN (JSON-матчи); оставшихся
    # наблюдений достаточно, чтобы выборочный шум не имитировал дрейф
    cur = rng.normal(0, 1, size=(4000, 1))
    cur[rng.random(4000) < 0.8, 0] = np.nan
    rep = psi_report(ref, cur, ["pos"])
    assert rep["pos"] < 0.1          # дрейфа нет
    # полностью NaN-колонка не роняет расчёт
    all_nan = np.full((100, 1), np.nan)
    rep2 = psi_report(ref, all_nan, ["pos"])
    assert "pos" in rep2  # равномерный fallback, без исключений


def test_train_with_nan_position_feature():
    """LightGBM обучается и предсказывает при NaN в части строк
    (смешанный датасет реплеи+JSON)."""
    import numpy as np
    from training.train_winprob import train, predict_calibrated

    ds = synth_matches(60, seed=8)
    ds.X = ds.X.copy()
    # половина матчей «из JSON» — позиции нет
    json_matches = {g for g in set(ds.groups.tolist()) if g % 2 == 0}
    mask = np.array([g in json_matches for g in ds.groups])
    pos_idx = FEATURES.index("position_advance")
    ds.X[mask, pos_idx] = np.nan
    art = train(ds, num_rounds=60)
    assert art["metrics"]["brier_calibrated"] < 0.3
    p = predict_calibrated(art, ds.X[:10])
    assert np.all((p >= 0) & (p <= 1)) and not np.any(np.isnan(p))


def test_gate_handles_feature_set_growth():
    """Гейт после добавления фич: старая prod (6 фич) честно оценивается на
    новой 9-колоночной матрице срезом до её набора — переход между
    поколениями фич не ломает продвижение."""
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from training.train_winprob import evaluate_gate, train

    ds = synth_matches(80, seed=13)
    new_art = train(ds, num_rounds=60)

    # «старая» модель: бустер видел только первые 6 фич
    X6 = ds.X[:, :6]
    booster = lgb.train({"objective": "binary", "verbose": -1},
                        lgb.Dataset(X6, label=ds.y), num_boost_round=30)
    raw = booster.predict(X6)
    cal = IsotonicRegression(y_min=0, y_max=1,
                             out_of_bounds="clip").fit(raw, ds.y)
    old_art = {"booster": booster.model_to_string(), "calibrator": cal,
               "features": FEATURES[:6]}
    ok, reason = evaluate_gate(new_art, old_art, ds)
    assert isinstance(ok, bool) and "одни данные" in reason


def test_gate_survives_a_feature_removed_from_the_middle():
    """Обратная сторона: фичу УДАЛИЛИ, и она была не последней.

    Ревизия трека F существует ровно ради удаления не оправдавших себя
    фич. Позиционная обрезка `X[:, :n]` этот случай не ломает — она молча
    сдвигает колонки, и старая production-модель получает networth там,
    где ждала xp. Гейт сравнил бы кандидата с испорченным соперником,
    объявил бы prod негодным и продвинул новую версию по ложному
    основанию: ни падения, ни следа в метриках.

    Здесь артефакт «старой» модели знает две фичи, которые НЕ являются
    первыми двумя в FEATURES. Позиционная трактовка дала бы ей другие
    колонки и другой ответ.
    """
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from training.train_winprob import predict_calibrated

    ds = synth_matches(60, seed=19)
    names = ["xp_diff", "networth_diff"]          # порядок и позиции чужие
    idx = [FEATURES.index(n) for n in names]
    assert idx != [0, 1], "тест обессмыслится, если это первые колонки"

    Xn = ds.X[:, idx]
    booster = lgb.train({"objective": "binary", "verbose": -1},
                        lgb.Dataset(Xn, label=ds.y), num_boost_round=30)
    cal = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(
        booster.predict(Xn), ds.y)
    art = {"booster": booster.model_to_string(), "calibrator": cal,
           "features": names}

    expected = cal.predict(booster.predict(Xn[:50]))
    got = predict_calibrated(art, ds.X[:50])
    assert np.allclose(got, expected), "колонки взяты не по именам"

    # Контрольный выстрел: позиционная трактовка дала бы ДРУГОЙ ответ,
    # иначе тест зелен при любом поведении кода.
    positional = cal.predict(booster.predict(ds.X[:50, :2]))
    assert not np.allclose(positional, expected)


def test_predict_calibrated_falls_back_to_positions_without_names():
    """У совсем древнего артефакта списка фич нет — догадываться не о чем,
    и позиционная трактовка остаётся единственно возможной."""
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from training.train_winprob import predict_calibrated

    ds = synth_matches(20, seed=3)
    booster = lgb.train({"objective": "binary", "verbose": -1},
                        lgb.Dataset(ds.X, label=ds.y), num_boost_round=10)
    cal = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(
        booster.predict(ds.X), ds.y)
    art = {"booster": booster.model_to_string(), "calibrator": cal}
    p = predict_calibrated(art, ds.X[:10])
    assert np.all((p >= 0) & (p <= 1))


def test_oof_calibration_and_platt_switch():
    """OOF-метрики в артефакте; калибратор по размеру: Platt < 50 матчей
    (train-часть), изотоника дальше — выбор зафиксирован в артефакте."""
    small = train(synth_matches(30, seed=17), num_rounds=40)
    assert small["metrics"]["calibrator"] == "platt"
    assert "platt" in small["algo"]
    big = train(synth_matches(90, seed=17), num_rounds=40)
    assert big["metrics"]["calibrator"] == "isotonic"
    for art in (small, big):
        m = art["metrics"]
        assert 0 < m["brier_oof"] < 0.3
        assert m["oof_folds"] >= 2
        assert m["best_iteration"] >= 1


def test_match_weights_equalize_matches():
    import numpy as np
    from training.train_winprob import _match_weights

    groups = np.array([1, 1, 1, 1, 2, 2])   # матч 1 — 4 строки, матч 2 — 2
    w = _match_weights(groups)
    # суммарный вес каждого матча одинаков
    assert abs(w[:4].sum() - w[4:].sum()) < 1e-9
    assert np.allclose(w[:4], 0.25) and np.allclose(w[4:], 0.5)


def test_gate_prefers_fresh_matches_holdout():
    """Если prod хранит max_match_id и в валидации ≥30 матчей новее — гейт
    сравнивает на них: эти матчи не видел ни prod (их не существовало), ни
    кандидат (валидация исключена из обучения)."""
    from training.train_winprob import evaluate_gate, train

    ds = synth_matches(200, seed=19)   # groups 1000..1199
    cand = train(ds, num_rounds=30)
    prod = dict(cand)
    prod["dataset"] = {**cand["dataset"], "max_match_id": 1005}
    ok, reason = evaluate_gate(cand, prod, ds)
    assert ok and "свежие матчи" in reason
    # без max_match_id (старый артефакт) — обычная валидация
    prod2 = dict(cand)
    prod2["dataset"] = {k: v for k, v in cand["dataset"].items()
                        if k != "max_match_id"}
    ok2, reason2 = evaluate_gate(cand, prod2, ds)
    assert ok2 and "валидация" in reason2
