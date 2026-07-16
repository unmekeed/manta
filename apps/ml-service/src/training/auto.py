"""Автономное переобучение Win Probability (Гл. 10.4: continuous training).

Цикл: раз в RETRAIN_INTERVAL_S проверить размер витрины. Если он изменился
на >= RETRAIN_MIN_NEW_MATCHES с момента последнего переобучения в этом
процессе (по модулю — устойчиво к сбросу/перестройке витрины) — переобучить
и загрузить в реестр; продвижение решает гейт по метрике на про-эталоне
(should_promote), поэтому деградация в сервинг не попадает даже при
полностью автономной работе. Первый прогон при наличии >= RETRAIN_MIN_TOTAL
матчей обучает сразу.

Уведомления о старте и каждом переобучении шлёт TelegramNotifier (см.
training.notify) — включается переменными TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Дрейф (Гл. 10.4, R-02): каждую итерацию считается PSI фич витрины против
эталона production-модели (метрики feature_psi/feature_psi_max); при
PSI >= RETRAIN_PSI_THRESHOLD переобучение запускается даже без прироста
матчей — «мета уехала», гейт решает судьбу кандидата как обычно.

Запуск: python -m training.auto [--once]
Env: RETRAIN_INTERVAL_S (21600), RETRAIN_MIN_NEW_MATCHES (20),
     RETRAIN_MIN_TOTAL_MATCHES (50), RETRAIN_PSI_THRESHOLD (0.25, 0=выкл),
     CLICKHOUSE_*, S3_* (реестр),
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (опционально).
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
from .drift import feature_psi
from .notify import TelegramNotifier
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
FEATURE_PSI = Gauge("feature_psi",
                    "PSI фичи: витрина против эталона production-модели",
                    ["feature"])
PSI_MAX = Gauge("feature_psi_max",
                "Максимальный PSI по фичам (>0.25 — дрейф, RB-ML-01)")

_notifier = TelegramNotifier()

# Размер датасета последнего переобучения В ЭТОМ ПРОЦЕССЕ. Триггер считает
# дельту относительно него, а НЕ относительно production: это устойчиво к
# сбросу/перестройке витрины (после сброса |n - last| снова растёт и обучение
# запустится, тогда как разница с production ушла бы в минус и застряла).
# None — в этом процессе ещё не обучались; первый прогон при n >= min_total
# запускает обучение сразу.
_last_trained_n: int | None = None


def _measure_drift(reg, ds) -> float | None:
    """PSI витрины против эталона production-артефакта; None — эталона нет
    (prod обучена до появления feature_reference или отсутствует)."""
    try:
        raw, _ = reg.resolve(MODEL_NAME, "production")
    except Exception:  # noqa: BLE001 — нет prod/реестра ⇒ дрейф не считаем
        return None
    import io

    art = joblib.load(io.BytesIO(raw))
    ref = art.get("feature_reference")
    if not ref:
        logger.info("production без feature_reference — PSI появится после "
                    "следующего продвижения")
        return None
    scores = feature_psi(ref, ds.X)
    for f, v in scores.items():
        FEATURE_PSI.labels(f).set(v)
    worst = max(scores.values(), default=0.0)
    PSI_MAX.set(worst)
    logger.info("PSI: %s (max %.3f)",
                {f: round(v, 3) for f, v in scores.items()}, worst)
    return worst


def check_and_train(min_new: int, min_total: int, out_path: Path,
                    psi_threshold: float = 0.25) -> str:
    """Одна итерация; возвращает статус для лога/теста."""
    global _last_trained_n
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
    PROD_MATCHES.set((prod or {}).get("dataset", {}).get("matches", 0))

    # Дрейф считается каждую итерацию (метрики нужны и без переобучения).
    worst_psi = _measure_drift(reg, ds)
    drifted = (psi_threshold > 0 and worst_psi is not None
               and worst_psi >= psi_threshold)

    if (_last_trained_n is not None
            and abs(ds.n_matches - _last_trained_n) < min_new):
        if not drifted:
            logger.info("изменение датасета %+d < %d (сейчас %d, в прошлый "
                        "раз обучались на %d) — пропуск",
                        ds.n_matches - _last_trained_n, min_new,
                        ds.n_matches, _last_trained_n)
            return "not-enough-new"
        # R-02: мета уехала (патч) — переобучаемся, не дожидаясь новых
        # матчей; гейт всё равно решает, попадёт ли версия в production.
        logger.warning("PSI %.3f >= %.2f — переобучение по дрейфу, хотя "
                       "новых матчей мало", worst_psi, psi_threshold)
        if _notifier.enabled:
            _notifier.send(f"📈 <b>Manta</b>: дрейф фич (PSI "
                           f"{worst_psi:.3f} ≥ {psi_threshold}) — "
                           f"внеплановое переобучение")

    delta = 0 if _last_trained_n is None else ds.n_matches - _last_trained_n
    logger.info("переобучение: %d матчей (изменение %+d с прошлого раза)",
                ds.n_matches, delta)
    artifact = train(ds)
    _last_trained_n = ds.n_matches
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out_path)
    logger.info("metrics: %s", artifact["metrics"])
    m = artifact["metrics"]
    BRIER_VALID.set(m.get("brier_calibrated", 0))
    if "brier_benchmark_pro" in m:
        BRIER_BENCHMARK.set(m["brier_benchmark_pro"])
    # Честный гейт: обе модели пересчитываются на общем holdout текущих данных
    # (evaluate_gate внутри push_with_gate) — устойчиво к «удачному» prod.
    _, promoted, reason = push_with_gate(artifact, out_path, logger, ds=ds)
    RETRAINS.labels("promoted" if promoted else "rejected").inc()
    if _notifier.enabled:
        _notifier.on_retrain(m, promoted, reason, ds.n_matches)
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
    psi_threshold = float(os.getenv("RETRAIN_PSI_THRESHOLD", "0.25"))
    out = Path(os.getenv("MODEL_OUT",
                         str(Path(__file__).resolve().parents[2]
                             / "models" / "win_probability.pkl")))

    metrics_port = int(os.getenv("METRICS_PORT", "9106"))
    if metrics_port and not args.once:
        start_http_server(metrics_port)
    logger.info("auto-train started: interval=%ds min_new=%d min_total=%d "
                "metrics=:%d", interval, min_new, min_total, metrics_port)
    if _notifier.enabled:
        _notifier.send("🚀 <b>Manta</b>: авто-обучение запущено\n" + _notifier.summary())
    while True:
        try:
            check_and_train(min_new, min_total, out, psi_threshold)
        except Exception:  # noqa: BLE001 — цикл живёт при сбоях зависимостей
            logger.exception("итерация auto-train упала; повтор через интервал")
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
