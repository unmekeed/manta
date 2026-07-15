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
                      merge, synth_matches)

logger = logging.getLogger("train_winprob")

MODEL_VERSION = "0.2.0"  # 0.2.0: + position_advance

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


def train(ds: Dataset, num_rounds: int = 300) -> dict:
    """Обучить модель + калибратор; вернуть артефакт со всеми метаданными."""
    (X_tr, y_tr), (X_va, y_va) = ds.split_by_match()
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
    return {
        "model_version": MODEL_VERSION,
        "algo": "lightgbm+isotonic",
        "features": FEATURES,
        "booster": booster.model_to_string(),
        "calibrator": calibrator,
        "metrics": metrics,
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
    """Решение промоушен-гейта (Гл. 10).

    Сравнение ТОЛЬКО по сопоставимым выборкам: приоритет — Brier на
    про-эталоне (фиксированная популяция tier-1 матчей); валидационный
    Brier версий с разными датасетами несопоставим (маленькая валидация
    льстит метрике). Если у production-версии эталонной метрики нет —
    её оценка не сопоставима с новой, продвигаем новую.
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


def push_with_gate(artifact: dict, out_path: Path, logger_) -> None:
    """Загрузить версию в реестр; продвинуть через гейт should_promote.
    Непродвинутая версия сохраняется и может быть продвинута вручную."""
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
    prod = reg.stage_metadata(MODEL_NAME)
    ok, reason = should_promote(artifact["metrics"],
                                prod.get("metrics") if prod else None)
    if ok:
        reg.promote(MODEL_NAME, version)
        logger_.info("registry: %s promoted (%s)", version, reason)
    else:
        logger_.warning("registry: %s NOT promoted (%s), версия сохранена",
                        version, reason)


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
        push_with_gate(artifact, out, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
