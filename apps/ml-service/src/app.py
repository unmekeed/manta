"""gRPC-сервер ML Service (Гл. 3.7, контракт proto/services.proto).

Реализует MLService:
- Predict       — вероятность победы Radiant по вектору фич;
- PredictStream — поток кадров матча → поток точек WP-кривой.

Модель — артефакт Win Probability (LightGBM + изотоническая калибровка,
см. training/train_winprob.py). Ключи FeatureVector.values должны
содержать фичи из training.dataset.FEATURES; отсутствующие ключи —
ошибка INVALID_ARGUMENT (молчаливый ноль исказил бы прогноз).

Запуск: PYTHONPATH=src python -m app [--port 50051] [--model PATH]
"""
from __future__ import annotations

import argparse
import logging
import os
from concurrent import futures

import grpc
import numpy as np
from prometheus_client import Counter, Histogram, start_http_server

from explain.winprob_shap import explain_matrix
from gen import services_pb2, services_pb2_grpc
from predictors.win_probability import DEFAULT_MODEL, WinProbability

logger = logging.getLogger("ml-service")

PREDICT_LATENCY = Histogram(
    "ml_predict_latency_seconds", "Латентность Predict/PredictStream (на кадр)",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1))
PREDICTIONS = Counter("ml_predictions_total", "Выполненные предсказания",
                      ["rpc"])


def _vector_from_features(fv, features: list[str]) -> np.ndarray:
    """FeatureVector → матрица (1, n) в порядке фич АРТЕФАКТА: сервер
    обслуживает ту версию модели, что загружена; лишние ключи клиента
    игнорируются (клиент новее модели — это нормально)."""
    missing = [f for f in features if f not in fv.values]
    if missing:
        raise KeyError(", ".join(missing))
    return np.array([[fv.values[f] for f in features]])


def _confidence(wp: float) -> float:
    """Грубая уверенность: расстояние от максимальной неопределённости 0.5,
    нормированное в [0, 1]. Честная оценка (квантили ансамбля) — Фаза 4."""
    return abs(wp - 0.5) * 2.0


class MLService(services_pb2_grpc.MLServiceServicer):
    def __init__(self, model: WinProbability,
                 extra_models: dict[str, WinProbability] | None = None):
        self.model = model
        # Дополнительные модели по model_name (Гл. 6.3: death_risk и т.п.).
        # Формат артефактов у всех одинаковый, сервятся тем же классом;
        # PredictResponse.win_probability_radiant несёт вероятность модели
        # (для death_risk — P(смерть в ближайшие 30 c)).
        self.extra = extra_models or {}

    def Predict(self, request, context):
        model = self.model
        if request.model_name not in ("", "win_probability"):
            model = self.extra.get(request.model_name)
            if model is None:
                context.abort(grpc.StatusCode.NOT_FOUND,
                              f"model {request.model_name!r} is not served")
        try:
            X = _vector_from_features(request.features, model.features)
        except KeyError as missing:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          f"missing features: {missing}")
        with PREDICT_LATENCY.time():
            wp = float(model.predict(X)[0])
        PREDICTIONS.labels("predict").inc()
        return services_pb2.PredictResponse(
            win_probability_radiant=wp,
            model_version=model.version,
        )

    def PredictStream(self, request_iterator, context):
        for frame in request_iterator:
            try:
                X = _vector_from_features(frame.features, self.model.features)
            except KeyError as missing:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                              f"frame t={frame.game_time}: missing features: "
                              f"{missing}")
            with PREDICT_LATENCY.time():
                wp = float(self.model.predict(X)[0])
                # SHAP кадра (TreeSHAP через pred_contrib — дёшево на
                # одном снапшоте): потребители прикладывают топ-вклады к
                # DetectedError в отчёте (Гл. 6.2, интерпретируемость).
                drivers = explain_matrix(self.model.booster, X,
                                         self.model.features, k=3)[0]
            PREDICTIONS.labels("stream").inc()
            yield services_pb2.WinProbability(
                game_time=frame.game_time,
                radiant=wp,
                confidence=_confidence(wp),
                top_contributions=[
                    services_pb2.FeatureContribution(
                        feature_name=name, contribution=val)
                    for name, val in drivers],
            )


def _resolve_model_path(spec: str | os.PathLike) -> str | os.PathLike:
    """`registry://name/ref` → скачать из реестра во временный файл;
    иначе — локальный путь как есть."""
    spec_s = str(spec)
    if not spec_s.startswith("registry://"):
        return spec
    import tempfile

    from registry import registry_from_env

    name, _, ref = spec_s[len("registry://"):].partition("/")
    artifact, meta = registry_from_env().resolve(name, ref or "production")
    tmp = tempfile.NamedTemporaryFile(suffix=".pkl", delete=False)
    tmp.write(artifact)
    tmp.close()
    logger.info("model resolved from registry: %s (%s)",
                meta.get("registry_version"), spec_s)
    return tmp.name


def build_server(model_path: str | os.PathLike, port: int) -> tuple[grpc.Server, int]:
    """Собрать сервер; port=0 выбирает свободный порт (для тестов)."""
    model = WinProbability(_resolve_model_path(model_path))
    # Дополнительные модели: NAME_MODEL_PATH из окружения (пусто/сбой —
    # сервим без них, Predict вернёт NOT_FOUND по этому имени).
    extra: dict[str, WinProbability] = {}
    for name, env, default in (
            ("death_risk", "DEATH_RISK_MODEL_PATH",
             "registry://death_risk/production"),
            ("laning", "LANING_MODEL_PATH", "registry://laning/production")):
        spec = os.getenv(env, default)
        if not spec:
            continue
        try:
            extra[name] = WinProbability(_resolve_model_path(spec))
            logger.info("extra model %s loaded (%s)", name, spec)
        except Exception as e:  # noqa: BLE001 — опциональная модель
            logger.warning("%s model unavailable (%s): %s", name, spec, e)
    # SO_REUSEPORT выключен: gRPC по умолчанию позволяет НЕСКОЛЬКИМ
    # процессам слушать один порт, и ядро молча балансирует соединения
    # между ними — задвоенный сервер со старой моделью отдавал бы часть
    # ответов незаметно. Пусть второй запуск падает с "address in use".
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         options=[("grpc.so_reuseport", 0)])
    services_pb2_grpc.add_MLServiceServicer_to_server(
        MLService(model, extra), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("GRPC_PORT", "50051")))
    ap.add_argument("--model", default=os.getenv("MODEL_PATH", str(DEFAULT_MODEL)))
    args = ap.parse_args()

    metrics_port = int(os.getenv("METRICS_PORT", "9104"))
    if metrics_port:
        start_http_server(metrics_port)
    server, port = build_server(args.model, args.port)
    server.start()
    logger.info("ml-service gRPC listening on :%d (model %s)", port, args.model)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
