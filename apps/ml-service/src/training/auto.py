"""Автономное переобучение Win Probability (Гл. 10.4: continuous training).

Цикл: раз в RETRAIN_INTERVAL_S проверить, сколько матчей прибавилось в
витринах со времени production-версии (метаданные реестра хранят размер
датасета каждой тренировки). Если новых матчей >= RETRAIN_MIN_NEW_MATCHES —
переобучить и загрузить в реестр; продвижение решает гейт по метрике на
про-эталоне (should_promote), поэтому деградация в сервинг не попадает
даже при полностью автономной работе.

Запуск: python -m training.auto [--once]
Env: RETRAIN_INTERVAL_S (21600), RETRAIN_MIN_NEW_MATCHES (20),
     RETRAIN_MIN_TOTAL_MATCHES (50), CLICKHOUSE_*, S3_* (реестр).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import joblib
from prometheus_client import Counter, Gauge, start_http_server

from registry import registry_from_env

from .dataset import load_from_clickhouse
from .train_winprob import MODEL_NAME, push_with_gate, train

logger = logging.getLogger("auto-train")

# Метрики обучения (Гл. 11.2.2: wp_brier_score_rolling и контур CT).
BRIER_BENCHMARK = Gauge("wp_brier_benchmark_pro",
                        "Brier последней тренировки на про-эталоне")
BRIER_VALID = Gauge("wp_brier_valid",
                    "Brier последней тренировки на валидации")
DATASET_MATCHES = Gauge("training_dataset_matches",
                        "Матчей в витрине на момент проверки")
PROD_MATCHES = Gauge("training_production_matches",
                     "Матчей в датасете production-версии")
RETRAINS = Counter("retrains_total", "Итоги переобучений",
                   ["outcome"])  # promoted | rejected


def check_and_train(min_new: int, min_total: int, out_path: Path) -> str:
    """Одна итерация; возвращает статус для лога/теста."""
    ds = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    DATASET_MATCHES.set(ds.n_matches)
    if ds.n_matches < min_total:
        logger.info("матчей %d < %d — рано обучать", ds.n_matches, min_total)
        return "not-enough-data"

    reg = registry_from_env()
    prod = reg.stage_metadata(MODEL_NAME)
    trained_on = (prod or {}).get("dataset", {}).get("matches", 0)
    PROD_MATCHES.set(trained_on)
    new_matches = ds.n_matches - trained_on
    if prod is not None and new_matches < min_new:
        logger.info("новых матчей %d < %d (в датасете %d, production обучена "
                    "на %d) — пропуск", new_matches, min_new,
                    ds.n_matches, trained_on)
        return "not-enough-new"

    logger.info("переобучение: %d матчей (+%d новых)", ds.n_matches,
                new_matches if prod else ds.n_matches)
    artifact = train(ds)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out_path)
    logger.info("metrics: %s", artifact["metrics"])
    m = artifact["metrics"]
    BRIER_VALID.set(m.get("brier_calibrated", 0))
    if "brier_benchmark_pro" in m:
        BRIER_BENCHMARK.set(m["brier_benchmark_pro"])
    before = reg.stage_metadata(MODEL_NAME)
    push_with_gate(artifact, out_path, logger)
    after = reg.stage_metadata(MODEL_NAME)
    promoted = (after or {}).get("registry_version") != (
        (before or {}).get("registry_version"))
    RETRAINS.labels("promoted" if promoted else "rejected").inc()
    return "trained"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"ml-autotrain","msg":"%(message)s"}')
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="одна проверка и выход (cron/тесты)")
    args = ap.parse_args()

    interval = int(os.getenv("RETRAIN_INTERVAL_S", "21600"))
    min_new = int(os.getenv("RETRAIN_MIN_NEW_MATCHES", "20"))
    min_total = int(os.getenv("RETRAIN_MIN_TOTAL_MATCHES", "50"))
    out = Path(os.getenv("MODEL_OUT",
                         str(Path(__file__).resolve().parents[2]
                             / "models" / "win_probability.pkl")))

    metrics_port = int(os.getenv("METRICS_PORT", "9106"))
    if metrics_port and not args.once:
        start_http_server(metrics_port)
    logger.info("auto-train started: interval=%ds min_new=%d min_total=%d "
                "metrics=:%d", interval, min_new, min_total, metrics_port)
    while True:
        try:
            check_and_train(min_new, min_total, out)
        except Exception:  # noqa: BLE001 — цикл живёт при сбоях зависимостей
            logger.exception("итерация auto-train упала; повтор через интервал")
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
