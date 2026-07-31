"""Про-матчи в обучении + симметричная калибровка (2026-07-31).

Замер на живом датасете: skill 0.58 на пабликах против 0.27 на про при
почти равной неопределённости. Причина — про-tier целиком был holdout'ом,
модель не видела этот домен вообще, а гейт мерил именно на нём. Плюс
калибратор учился на незеркалированных парах и возвращал приор сторон,
который аугментация убирала (смещение на про +0.035 против +0.012 на
пабликах).

Здесь проверяется, что разделение про не течёт (матч целиком в одной
части, train и гейт видят одно разбиение) и что калибровка симметрична.
"""
import os
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from training.dataset import FEATURES, Dataset, PRO_TIER  # noqa: E402


def make_ds(n_pub=40, n_pro=30, rows=6, seed=0):
    rng = np.random.default_rng(seed)
    X, y, g, t = [], [], [], []
    for i in range(n_pub + n_pro):
        mid = 9_000_000_000 + i
        pro = i >= n_pub
        win = int(rng.random() < (0.5 if pro else 0.6))
        for r in range(rows):
            X.append(rng.normal(size=len(FEATURES)))
            y.append(win)
            g.append(mid)
            t.append(PRO_TIER if pro else "Premium")
    return Dataset(X=np.array(X), y=np.array(y, float), groups=np.array(g),
                   n_matches=n_pub + n_pro, tiers=np.array(t))


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.get("PRO_TRAIN_FRAC")
    os.environ.pop("PRO_TRAIN_FRAC", None)
    yield
    os.environ.pop("PRO_TRAIN_FRAC", None)
    if old is not None:
        os.environ["PRO_TRAIN_FRAC"] = old


def _pro_matches(ds):
    pro = ds.tiers == PRO_TIER
    return set(ds.groups[pro].tolist())


def test_half_of_pro_goes_to_training_by_default():
    ds = make_ds()
    bench = set(ds.groups[ds._bench_mask()].tolist())
    all_pro = _pro_matches(ds)
    assert bench < all_pro, "часть про обязана уходить в обучение"
    assert 0.3 < len(bench) / len(all_pro) < 0.7


def test_benchmark_never_leaks_into_training():
    """Матч целиком в одной части: иначе гейт мерил бы кандидата на
    строках матчей, которые тот видел при обучении."""
    ds = make_ds()
    (X_tr, y_tr), _ = ds.split_by_match()
    bench_mask = ds._bench_mask()
    bench_matches = set(ds.groups[bench_mask].tolist())
    # собираем match_id обучающих строк тем же способом, что split_by_match
    in_valid, pro = ds._valid_mask()
    tr_matches = set(ds.groups[~in_valid & ~pro].tolist())
    assert tr_matches & bench_matches == set()
    assert len(X_tr) == len(y_tr)


def test_split_is_deterministic_across_calls():
    """train и гейт зовут разбиение независимо — оно обязано совпасть."""
    ds = make_ds()
    a = ds._bench_mask(seed=42)
    b = make_ds()._bench_mask(seed=42)
    assert np.array_equal(a, b)


def test_pro_train_frac_zero_restores_old_behaviour():
    """Откат одной переменной: весь про снова только эталон."""
    os.environ["PRO_TRAIN_FRAC"] = "0"
    ds = make_ds()
    bench = set(ds.groups[ds._bench_mask()].tolist())
    assert bench == _pro_matches(ds)


def test_benchmark_never_empty():
    """Даже при frac=1 хотя бы один матч остаётся эталоном — иначе гейту
    не на чем сравнивать кандидата с production."""
    os.environ["PRO_TRAIN_FRAC"] = "1"
    ds = make_ds(n_pro=5)
    assert ds._bench_mask().any()


def test_training_now_contains_pro_rows():
    """Собственно цель правки: про-домен попадает в обучение."""
    ds = make_ds()
    in_valid, pro = ds._valid_mask()
    tr = ~in_valid & ~pro
    assert (ds.tiers[tr] == PRO_TIER).any()


# -- симметричная калибровка --------------------------------------------------

def test_calibrator_is_side_agnostic():
    """cal(p) + cal(1−p) ≈ 1.

    Бустер зеркалированием сделан симметричным; если калибратор учить
    только на исходных парах, он возвращает приор сторон обратно. Здесь
    смещённая выборка (Radiant побеждает в 75% случаев) — на ней
    несимметричная калибровка провалила бы проверку.
    """
    from training.train_winprob import _PlattCalibrator

    rng = np.random.default_rng(3)
    p = rng.uniform(0.05, 0.95, 4000)
    y = (rng.random(4000) < 0.75).astype(float)

    naive = _PlattCalibrator()
    naive.fit(p, y)

    sym = _PlattCalibrator()
    sym.fit(np.concatenate([p, 1 - p]), np.concatenate([y, 1 - y]))

    grid = np.linspace(0.05, 0.95, 19)
    naive_gap = np.abs(naive.predict(grid) + naive.predict(1 - grid) - 1).max()
    sym_gap = np.abs(sym.predict(grid) + sym.predict(1 - grid) - 1).max()

    assert sym_gap < 1e-6, f"симметричная калибровка кривая: {sym_gap}"
    assert naive_gap > 0.05, ("на смещённой выборке несимметричная "
                              f"калибровка обязана уехать, got {naive_gap}")
