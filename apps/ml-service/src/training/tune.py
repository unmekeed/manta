"""Подбор гиперпараметров Win Probability через Optuna (трек F, F7).

Зачем. LGB_PARAMS подбирались вручную под режим «строк много, матчей
мало» (спринт 33) и с тех пор не пересматривались, хотя число фич выросло
с 5 до 22. Optuna ищет по OOF-Brier — той же метрике, что в гейте, и на
том же group-split по матчам, поэтому результат не оптимистичен.

Что НЕ подбирается: монотонные ограничения (доменное знание, а не
гиперпараметр) и число раундов (его выбирает early stopping внутри
фолдов).

Итог печатается как готовый блок LGB_PARAMS и, с --apply, записывается в
models/lgb_params.json — train_winprob подхватывает его при следующем
обучении. Автоматически в прод ничего не уезжает: гейт всё равно решает.

CLI: python -m training.tune [--trials 60] [--apply]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger("tune")

PARAMS_PATH = Path(__file__).resolve().parents[2] / "models" / "lgb_params.json"

# Минимальное улучшение OOF-Brier, ради которого стоит менять параметры.
# Бутстрап-σ разницы версий на нашем объёме данных — порядка 0.003
# (спринт 27), поэтому выигрыш в тысячные доли — шум перебора, а не
# качество: 40 траялов на одной валидации сами по себе дают такую
# «победу». Ниже порога файл не пишется, ручные параметры остаются.
MIN_IMPROVEMENT = 0.002


def load_overrides() -> dict:
    """Подобранные параметры, если они есть (читает train_winprob)."""
    try:
        return json.loads(PARAMS_PATH.read_text())
    except Exception:  # noqa: BLE001 — файла нет: дефолты в коде
        return {}


def objective_factory(ds, folds: int = 4, seed: int = 42):
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss

    from .dataset import FEATURES, mirror_xy
    from .train_winprob import MONOTONE, _match_weights

    in_valid, pro = ds._valid_mask()
    tr = ~in_valid & ~pro
    X, y, g = ds.X[tr], ds.y[tr], ds.groups[tr]
    pw = ds.patch_weights()[tr]
    matches = np.array(sorted(set(g.tolist())))
    rng = np.random.default_rng(seed)
    rng.shuffle(matches)
    chunks = np.array_split(matches, folds)
    mono = [MONOTONE[f] for f in FEATURES]

    def objective(trial) -> float:
        params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "num_leaves": trial.suggest_int("num_leaves", 8, 64),
            "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 20, 300),
            "lambda_l2": trial.suggest_float("lambda_l2", 0.1, 50.0, log=True),
            "lambda_l1": trial.suggest_float("lambda_l1", 1e-3, 10.0, log=True),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.12,
                                                 log=True),
            "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
            "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
            "bagging_freq": trial.suggest_int("bagging_freq", 0, 5),
            "monotone_constraints": mono,
            "verbose": -1,
            "seed": seed,
        }
        oof = np.full(len(y), np.nan)
        for fold in chunks:
            va = np.isin(g, fold)
            trn = ~va
            if va.sum() == 0 or trn.sum() == 0:
                continue
            Xt, yt = X[trn], y[trn]
            w = _match_weights(g[trn]) * pw[trn]
            Xt, yt = mirror_xy(Xt, yt)
            w = np.concatenate([w, w])
            b = lgb.train(params,
                          lgb.Dataset(Xt, label=yt, weight=w,
                                      feature_name=FEATURES),
                          num_boost_round=400,
                          valid_sets=[lgb.Dataset(X[va], label=y[va])],
                          callbacks=[lgb.early_stopping(30, verbose=False)])
            oof[va] = b.predict(X[va])
        seen = ~np.isnan(oof)
        if seen.sum() < 100:
            return 1.0
        cal = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        cal.fit(oof[seen], y[seen])
        return float(brier_score_loss(y[seen], cal.predict(oof[seen])))

    return objective


def main() -> int:
    import optuna

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--apply", action="store_true",
                    help="записать найденные параметры в models/lgb_params.json")
    args = ap.parse_args()

    from .dataset import load_from_clickhouse
    from .train_winprob import LGB_PARAMS

    ds = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    logger.info("датасет: %d матчей, %d строк", ds.n_matches, len(ds.y))

    objective = objective_factory(ds)
    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    # Первым делом пробуем текущие ручные параметры — чтобы знать базу и
    # не «улучшить» модель в худшую сторону.
    study.enqueue_trial({k: LGB_PARAMS[k] for k in
                         ("num_leaves", "min_data_in_leaf", "lambda_l2",
                          "learning_rate", "feature_fraction")
                         if k in LGB_PARAMS} | {"lambda_l1": 0.001,
                                                "bagging_fraction": 1.0,
                                                "bagging_freq": 0})
    study.optimize(objective, n_trials=args.trials, show_progress_bar=False)

    base = study.trials[0].value
    best = study.best_value
    logger.info("текущие параметры: OOF-Brier %.5f", base)
    logger.info("лучшие найденные:  OOF-Brier %.5f (улучшение %.5f)",
                best, base - best)
    logger.info("параметры: %s", json.dumps(study.best_params, indent=2))

    if args.apply:
        if best < base - MIN_IMPROVEMENT:
            PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
            PARAMS_PATH.write_text(json.dumps(study.best_params, indent=1))
            logger.info("записано в %s — следующее обучение их подхватит",
                        PARAMS_PATH)
        else:
            logger.info("улучшение %.5f < порога %.3f — файл НЕ записан "
                        "(перебор на одной валидации даёт такой выигрыш "
                        "случайно; ручные параметры остаются)",
                        base - best, MIN_IMPROVEMENT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
