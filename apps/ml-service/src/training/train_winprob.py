"""Обучение бейзлайна Win Probability (Гл. 6.2.2).

Стек по спецификации: LightGBM (binary) + калибратор поверх сырого выхода
(изотоническая регрессия на отложенных матчах). Метрика приёмки — Brier;
целевой порог из спецификации: ≤ 0.18 на реальных данных.

Запуск:
    python -m training.train_winprob [--synthetic N] [--out models/win_probability.pkl]

Реальные матчи читаются из MatchTimelineFeatures; пока их мало, добавка
--synthetic N дополняет датасет синтетикой (факт фиксируется в метаданных
артефакта — модель со синтетикой не должна попадать в прод).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss

from .dataset import (FEATURES, Dataset, dataset_hash, load_from_clickhouse,
                      merge, mirror_xy, synth_matches)

logger = logging.getLogger("train_winprob")

MODEL_VERSION = "0.4.0"  # 0.4.0: feature_reference для PSI-дрейфа

# Монотонные ограничения — доменное знание (Гл. 6.2.2): вероятность
# победы Radiant не убывает по преимуществу в золоте/опыте/убийствах и
# территории; по времени и суммарным убийствам знак не фиксирован.
# Порядок соответствует dataset.FEATURES.
MONOTONE = {
    "game_time": 0,
    "networth_diff": 1,
    "xp_diff": 1,
    "kills_diff": 1,
    "kills_total": 0,
    "position_advance": 1,
}

LGB_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "monotone_constraints": [MONOTONE[f] for f in FEATURES],
    "verbose": -1,
    "seed": 42,
}


def train(ds: Dataset, num_rounds: int = 300, mirror: bool = True) -> dict:
    """Обучить модель + калибратор; вернуть артефакт со всеми метаданными.

    mirror=True (по умолчанию): train-часть зеркалируется по сторонам
    (dataset.mirror_xy) — модель становится side-agnostic, приор стороны
    обнуляется. Валидация и эталон НЕ зеркалируются (оценка в исходной
    ориентации).
    """
    (X_tr, y_tr), (X_va, y_va) = ds.split_by_match()
    if mirror:
        X_tr, y_tr = mirror_xy(X_tr, y_tr)
    booster = lgb.train(
        LGB_PARAMS,
        lgb.Dataset(X_tr, label=y_tr, feature_name=FEATURES),
        num_boost_round=num_rounds,
        valid_sets=[lgb.Dataset(X_va, label=y_va)],
        callbacks=[lgb.early_stopping(30, verbose=False)],
    )
    raw_va = booster.predict(X_va)

    # Калибровка (Гл. 6.2.2): изотоническая регрессия на отложенных матчах.
    calibrator = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    calibrator.fit(raw_va, y_va)
    cal_va = calibrator.predict(raw_va)

    metrics = {
        "brier_raw": round(float(brier_score_loss(y_va, raw_va)), 4),
        "brier_calibrated": round(float(brier_score_loss(y_va, cal_va)), 4),
        "logloss_calibrated": round(float(log_loss(y_va, np.clip(cal_va, 1e-6, 1 - 1e-6))), 4),
        "valid_rows": int(len(y_va)),
        "best_iteration": int(booster.best_iteration or num_rounds),
    }

    # Эталон: матчи про-команд (tier=Professional) — вне train/valid.
    # Метрика на них показывает перенос модели, обученной на high-rank
    # пабликах, на тир-1 игру; в гейте продвижения НЕ участвует (пока
    # эталонная выборка мала), но фиксируется в каждой версии.
    X_bm, y_bm = ds.benchmark()
    if len(y_bm) > 0:
        cal_bm = calibrator.predict(booster.predict(X_bm))
        metrics["brier_benchmark_pro"] = round(
            float(brier_score_loss(y_bm, cal_bm)), 4)
        metrics["benchmark_rows"] = int(len(y_bm))
    from .drift import reference_hist

    return {
        "model_version": MODEL_VERSION,
        "algo": "lightgbm+isotonic+mirror",
        "features": FEATURES,
        "booster": booster.model_to_string(),
        "calibrator": calibrator,
        "metrics": metrics,
        # Эталон распределений фич для PSI-дрейфа (training.drift):
        # по нему auto-train сравнивает будущую витрину с тем, что видела
        # эта модель при обучении. Сырые данные, без зеркалирования.
        "feature_reference": reference_hist(ds.X),
        "dataset": {
            "matches": ds.n_matches,
            "synthetic_matches": ds.n_synthetic,
            "rows": int(len(ds.y)),
            "hash": dataset_hash(ds),
        },
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


MODEL_NAME = "win_probability"


def should_promote(new_metrics: dict, prod_metrics: dict | None) -> tuple[bool, str]:
    """Гейт по СОХРАНЁННЫМ метрикам (легаси-путь / нет доступа к данным).

    Используется как fallback, когда честное пересравнение на одних данных
    невозможно (нет датасета под рукой). Приоритет — Brier на про-эталоне;
    иначе валидационный Brier. ВНИМАНИЕ: валидации разных версий считаются на
    разных сплитах и строго несопоставимы — поэтому основной путь гейта теперь
    evaluate_gate() (пересчёт обеих моделей на общем holdout).
    """
    if prod_metrics is None:
        return True, "первая версия"
    new_bm = new_metrics.get("brier_benchmark_pro")
    prod_bm = prod_metrics.get("brier_benchmark_pro")
    if new_bm is not None and prod_bm is not None:
        if new_bm <= prod_bm:
            return True, f"benchmark {new_bm:.4f} <= prod {prod_bm:.4f}"
        return False, f"benchmark {new_bm:.4f} > prod {prod_bm:.4f}"
    if new_bm is not None and prod_bm is None:
        return True, ("у production нет эталонной метрики — "
                      "валидации несопоставимы, продвигаем оцененную на эталоне")
    # Эталона нет ни у кого — остаётся валидационный Brier.
    new_v = new_metrics.get("brier_calibrated", float("inf"))
    prod_v = prod_metrics.get("brier_calibrated", float("inf"))
    if new_v <= prod_v:
        return True, f"valid {new_v:.4f} <= prod {prod_v:.4f}"
    return False, f"valid {new_v:.4f} > prod {prod_v:.4f}"


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def predict_calibrated(model: dict, X: np.ndarray) -> np.ndarray:
    """Калиброванная WP по артефакту (booster-строка + изотоника)."""
    booster = lgb.Booster(model_str=model["booster"])
    return model["calibrator"].predict(booster.predict(X))


def _paired_bootstrap_delta(y, p_new, p_prod, groups, n_boot: int = 200,
                            seed: int = 42) -> tuple[float, float]:
    """Bootstrap ПО МАТЧАМ разницы Brier(new) − Brier(prod).

    Ресэмплим матчи целиком (снапшоты одного матча скоррелированы), считаем
    Δ на каждой выборке — получаем точечную оценку и разброс (шум метрики на
    этом holdout). Возвращает (delta_point, delta_std).
    """
    delta_point = _brier(y, p_new) - _brier(y, p_prod)
    uniq = np.array(sorted(set(groups.tolist())))
    if len(uniq) < 3:
        return delta_point, 0.0
    idx_by_g = {int(g): np.where(groups == g)[0] for g in uniq}
    rng = np.random.default_rng(seed)
    deltas = []
    for _ in range(n_boot):
        sample = rng.choice(uniq, size=len(uniq), replace=True)
        rows = np.concatenate([idx_by_g[int(g)] for g in sample])
        deltas.append(_brier(y[rows], p_new[rows]) - _brier(y[rows], p_prod[rows]))
    return delta_point, float(np.std(deltas))


def evaluate_gate(new_art: dict, prod_art: dict, ds,
                  tol_floor: float = 0.0005) -> tuple[bool, str]:
    """Честный гейт: обе модели считаются на ОДНОМ holdout текущих данных.

    Убирает залипание на «удачном» маленьком prod-датасете — production
    пересчитывается на актуальной выборке, а не сравнивается по своей старой
    сохранённой метрике. Продвигаем, если кандидат НЕ ЗНАЧИМО хуже: Δ Brier в
    пределах шума (± bootstrap-σ по матчам). При равенстве в пределах шума
    предпочитаем новую версию — она обучена на бОльших данных и устойчивее.
    """
    X, y, groups, kind = ds.eval_holdout()
    if len(y) == 0:
        return True, "нет общего holdout — продвигаем"
    p_new = predict_calibrated(new_art, X)
    p_prod = predict_calibrated(prod_art, X)
    b_new, b_prod = _brier(y, p_new), _brier(y, p_prod)
    delta, std = _paired_bootstrap_delta(y, p_new, p_prod, groups)
    tol = max(tol_floor, std)
    ok = delta <= tol
    label = "про-эталон" if kind == "benchmark_pro" else "валидация"
    n_m = len(set(groups.tolist()))
    verdict = "не хуже prod" if ok else "значимо хуже prod"
    reason = (f"{label}, одни данные ({n_m} матчей): new {b_new:.4f} vs "
              f"prod {b_prod:.4f} (Δ{delta:+.4f}, σ{std:.4f}) — {verdict}")
    return ok, reason


def push_with_gate(artifact: dict, out_path: Path, logger_, ds=None
                   ) -> tuple[str, bool, str]:
    """Загрузить версию в реестр; продвинуть через гейт.

    Если передан ds — гейт честный (evaluate_gate: обе модели на общем
    holdout текущих данных). Без ds — легаси-fallback по сохранённым метрикам
    (should_promote). Непродвинутая версия сохраняется в реестре.
    Возвращает (version, promoted, reason).
    """
    from registry import registry_from_env

    reg = registry_from_env()
    version = reg.push(MODEL_NAME, out_path.read_bytes(), {
        "model_version": artifact["model_version"],
        "algo": artifact["algo"],
        "features": artifact["features"],
        "metrics": artifact["metrics"],
        "dataset": artifact["dataset"],
        "trained_at": artifact["trained_at"],
    })
    try:
        prod_bytes, _ = reg.resolve(MODEL_NAME, "production")
    except KeyError:
        prod_bytes = None

    if prod_bytes is None:
        ok, reason = True, "первая версия"
    elif ds is not None:
        prod_art = joblib.load(io.BytesIO(prod_bytes))
        ok, reason = evaluate_gate(artifact, prod_art, ds)
    else:
        prod = reg.stage_metadata(MODEL_NAME)
        ok, reason = should_promote(artifact["metrics"],
                                    prod.get("metrics") if prod else None)
    if ok:
        reg.promote(MODEL_NAME, version)
        logger_.info("registry: %s promoted (%s)", version, reason)
    else:
        logger_.warning("registry: %s NOT promoted (%s), версия сохранена",
                        version, reason)
    return version, ok, reason


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", type=int, default=0,
                    help="добавить N синтетических матчей (smoke-режим)")
    ap.add_argument("--min-matches", type=int, default=20)
    ap.add_argument("--push", action="store_true",
                    help="загрузить в реестр моделей (промоушен по Brier-гейту)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2]
                                         / "models" / "win_probability.pkl"))
    args = ap.parse_args()

    real = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    logger.info("real matches: %d (%d rows)", real.n_matches, len(real.y))

    ds = real
    if args.synthetic > 0:
        ds = merge(real, synth_matches(args.synthetic))
        logger.info("added %d synthetic matches", args.synthetic)

    if ds.n_matches < args.min_matches:
        logger.error("недостаточно матчей: %d < %d (добавьте --synthetic N "
                     "для smoke-прогона)", ds.n_matches, args.min_matches)
        return 1

    artifact = train(ds)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out)
    logger.info("metrics: %s", json.dumps(artifact["metrics"]))
    logger.info("artifact saved: %s (%.1f KiB)", out, out.stat().st_size / 1024)
    if args.push:
        push_with_gate(artifact, out, logger, ds=ds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
