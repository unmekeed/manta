"""Автономное переобучение Win Probability (Гл. 10.4: continuous training).

Цикл: раз в RETRAIN_INTERVAL_S проверить размер витрины. Переобучение
запускается, если сработал ЛЮБОЙ из триггеров:

- объём: размер витрины изменился на >= RETRAIN_MIN_NEW_MATCHES с момента
  последнего переобучения в этом процессе (по модулю — устойчиво к
  сбросу/перестройке витрины);
- дрейф (Гл. 10.4, риск R-02): PSI распределения фич текущей витрины
  против референса production-модели превысил RETRAIN_PSI_THRESHOLD —
  типично после баланс-патча Dota: матчей может быть мало, но игра уже
  другая, и ждать полного порога объёма опасно.

Продвижение решает честный гейт (evaluate_gate), поэтому деградация в
сервинг не попадает даже при полностью автономной работе. Первый прогон при
наличии >= RETRAIN_MIN_TOTAL матчей обучает сразу.

Уведомления о старте и каждом переобучении шлёт TelegramNotifier (см.
training.notify) — включается переменными TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Запуск: python -m training.auto [--once]
Env: RETRAIN_INTERVAL_S (21600), RETRAIN_MIN_NEW_MATCHES (20),
     RETRAIN_MIN_TOTAL_MATCHES (50), RETRAIN_PSI_THRESHOLD (0.2),
     CLICKHOUSE_*, S3_* (реестр), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import joblib
import requests
from prometheus_client import Counter, Gauge, start_http_server

from registry import registry_from_env

from .dataset import FEATURES, load_from_clickhouse
from .drift import PSI_SIGNIFICANT, max_psi, psi_report
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
FEATURE_PSI = Gauge("wp_feature_psi",
                    "PSI фичи: текущая витрина против референса production",
                    ["feature"])
PSI_MAX = Gauge("wp_psi_max", "Максимальный PSI по фичам")
BRIER_PHASE = Gauge("wp_brier_phase",
                    "Brier последней тренировки по фазам игры",
                    ["phase"])  # early | mid | late

_notifier = TelegramNotifier()

# Размер датасета последнего переобучения В ЭТОМ ПРОЦЕССЕ. Триггер считает
# дельту относительно него, а НЕ относительно production: это устойчиво к
# сбросу/перестройке витрины (после сброса |n - last| снова растёт и обучение
# запустится, тогда как разница с production ушла бы в минус и застряла).
# None — в этом процессе ещё не обучались; первый прогон при n >= min_total
# запускает обучение сразу.
_last_trained_n: int | None = None

# Стагнация витрины (runbook «витрина не растёт»: rate limit OpenDota, упавший
# коллектор/экстрактор). Алерт шлётся один раз на эпизод, повторный — после
# возобновления роста.
_last_growth = (None, 0.0)   # (последний размер, время его изменения)
_stall_alerted = False
DATASET_STALLED = Gauge("training_dataset_stalled",
                        "1 — витрина не растёт дольше DATASET_STALL_ALERT_H")


def _check_stall(n_matches: int) -> None:
    global _last_growth, _stall_alerted
    now = time.time()
    last_n, since = _last_growth
    if n_matches != last_n:
        _last_growth = (n_matches, now)
        if _stall_alerted and _notifier.enabled:
            _notifier.send("✅ <b>Manta</b>: витрина снова растёт "
                           f"({n_matches} матчей)")
        _stall_alerted = False
        DATASET_STALLED.set(0)
        return
    stall_h = float(os.getenv("DATASET_STALL_ALERT_H", "12"))
    if now - since >= stall_h * 3600:
        DATASET_STALLED.set(1)
        if not _stall_alerted:
            _stall_alerted = True
            logger.warning("витрина не растёт %.1f ч (застыла на %d матчах)",
                           (now - since) / 3600, n_matches)
            if _notifier.enabled:
                _notifier.send(
                    "⚠️ <b>Manta</b>: витрина не растёт "
                    f"{(now - since) / 3600:.0f} ч (застыла на {n_matches} "
                    "матчах).\nЧастые причины — docs/runbooks.md: суточный "
                    "rate limit OpenDota, упавший коллектор/экстрактор.")


# Реплейный путь (инцидент №6 HANDOFF): при потере Kafka-топиков ReplayEvents
# перестаёт наполняться, но витрина продолжает расти за счёт JSON-пути —
# stall-алерт выше этого НЕ ловит. Свежесть реплейной таблицы проверяется
# отдельно; алерт один на эпизод, с сообщением о восстановлении.
_replay_alerted = False
REPLAY_STALLED = Gauge(
    "training_replay_path_stalled",
    "1 — ReplayEvents не пополняется дольше REPLAY_STALL_ALERT_H")


def _replay_freshness_ts() -> float | None:
    """Unix-время последней вставки в ReplayEvents (0.0 — таблица пуста);
    None — ClickHouse недоступен: это отдельная проблема, не алертим."""
    try:
        r = requests.post(
            os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
            params={"database": os.getenv("CLICKHOUSE_DB", "manta")},
            headers={
                "X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
                "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                              "dota_dev_password")},
            data="SELECT toUnixTimestamp(max(ingested_at)) FROM ReplayEvents",
            timeout=10)
        r.raise_for_status()
        return float(r.text.strip() or 0)
    except Exception:  # noqa: BLE001
        return None


def _check_replay_stall() -> None:
    global _replay_alerted
    ts = _replay_freshness_ts()
    if ts is None:
        return
    age_h = (time.time() - ts) / 3600
    stall_h = float(os.getenv("REPLAY_STALL_ALERT_H", "6"))
    if ts > 0 and age_h < stall_h:
        if _replay_alerted and _notifier.enabled:
            _notifier.send("✅ <b>Manta</b>: реплейный путь снова пишет "
                           "ReplayEvents")
        _replay_alerted = False
        REPLAY_STALLED.set(0)
        return
    REPLAY_STALLED.set(1)
    if not _replay_alerted:
        _replay_alerted = True
        what = ("таблица ReplayEvents пуста — путь никогда не писал"
                if ts == 0 else f"последняя вставка {age_h:.0f} ч назад")
        logger.warning("реплейный путь стоит: %s", what)
        if _notifier.enabled:
            _notifier.send(
                f"⚠️ <b>Manta</b>: реплейный путь стоит — {what}.\n"
                "Витрина может расти по JSON-пути и маскировать это. "
                "Диагностика: make doctor; частая причина — потерянные "
                "Kafka-топики (docs/runbooks.md).")


def check_and_train(min_new: int, min_total: int, out_path: Path) -> str:
    """Одна итерация; возвращает статус для лога/теста."""
    global _last_trained_n
    ds = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    DATASET_MATCHES.set(ds.n_matches)
    _check_stall(ds.n_matches)
    _check_replay_stall()
    if ds.n_matches < min_total:
        logger.info("матчей %d < %d — рано обучать", ds.n_matches, min_total)
        return "not-enough-data"

    reg = registry_from_env()
    prod = reg.stage_metadata(MODEL_NAME)
    PROD_MATCHES.set((prod or {}).get("dataset", {}).get("matches", 0))

    # Дрейф фич: PSI текущей витрины против референса production-модели
    # (компактные децильные гистограммы в метаданных версии). Старые версии
    # без референса дрейф-триггер не активируют — только метрика объёма.
    drift_max = 0.0
    reference = (prod or {}).get("drift_reference") or {}
    if reference and len(ds.y) > 0:
        report = psi_report(reference, ds.X, FEATURES)
        for name, val in report.items():
            FEATURE_PSI.labels(name).set(val)
        drift_max = max_psi(report)
        PSI_MAX.set(drift_max)
        logger.info("PSI против production: max %.3f %s", drift_max, report)

    psi_threshold = float(os.getenv("RETRAIN_PSI_THRESHOLD",
                                    str(PSI_SIGNIFICANT)))
    enough_new = (_last_trained_n is None
                  or abs(ds.n_matches - _last_trained_n) >= min_new)
    # Дрейф триггерит, только если витрина изменилась с последнего обучения
    # В ЭТОМ ПРОЦЕССЕ: иначе (гейт отклонил кандидата, дрейф остался)
    # переобучение на тех же данных дало бы ту же модель каждый цикл.
    drifted = (drift_max >= psi_threshold
               and ds.n_matches != _last_trained_n)
    if not enough_new and not drifted:
        logger.info("изменение датасета %+d < %d (сейчас %d, в прошлый раз "
                    "обучались на %d), PSI %.3f < %.2f — пропуск",
                    ds.n_matches - _last_trained_n, min_new,
                    ds.n_matches, _last_trained_n, drift_max, psi_threshold)
        return "not-enough-new"

    delta = 0 if _last_trained_n is None else ds.n_matches - _last_trained_n
    trigger = "drift" if (drifted and not enough_new) else "volume"
    logger.info("переобучение [%s]: %d матчей (изменение %+d, PSI %.3f)",
                trigger, ds.n_matches, delta, drift_max)
    artifact = train(ds)
    _last_trained_n = ds.n_matches
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out_path)
    logger.info("metrics: %s", artifact["metrics"])
    m = artifact["metrics"]
    BRIER_VALID.set(m.get("brier_calibrated", 0))
    if "brier_benchmark_pro" in m:
        BRIER_BENCHMARK.set(m["brier_benchmark_pro"])
    for phase in ("early", "mid", "late"):
        if f"brier_{phase}" in m:
            BRIER_PHASE.labels(phase).set(m[f"brier_{phase}"])
    # Честный гейт: обе модели пересчитываются на общем holdout текущих данных
    # (evaluate_gate внутри push_with_gate) — устойчиво к «удачному» prod.
    _, promoted, reason = push_with_gate(artifact, out_path, logger, ds=ds)
    RETRAINS.labels("promoted" if promoted else "rejected").inc()
    # D2: реестр растёт на ~4 версии/день — держим последние N + все
    # продвигавшиеся. MLflow-бэкенд управляет хранением сам (cleanup нет).
    keep_last = int(os.getenv("REGISTRY_KEEP_LAST", "10"))
    if keep_last and hasattr(reg, "cleanup"):
        removed = reg.cleanup(MODEL_NAME, keep_last=keep_last)
        if removed:
            logger.info("реестр: удалено %d старых версий (%s ... %s)",
                        len(removed), removed[0], removed[-1])
    if trigger == "drift":
        reason = f"⚠️ дрейф фич (PSI {drift_max:.2f}) → {reason}"
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
            check_and_train(min_new, min_total, out)
        except Exception:  # noqa: BLE001 — цикл живёт при сбоях зависимостей
            logger.exception("итерация auto-train упала; повтор через интервал")
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
