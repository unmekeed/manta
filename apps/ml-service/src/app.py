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

from gen import services_pb2, services_pb2_grpc
from predictors.win_probability import DEFAULT_MODEL, WinProbability
from training.dataset import FEATURES

logger = logging.getLogger("ml-service")


def _vector_from_features(fv) -> np.ndarray:
    """FeatureVector → матрица (1, n) в порядке FEATURES."""
    missing = [f for f in FEATURES if f not in fv.values]
    if missing:
        raise KeyError(", ".join(missing))
    return np.array([[fv.values[f] for f in FEATURES]])


def _confidence(wp: float) -> float:
    """Грубая уверенность: расстояние от максимальной неопределённости 0.5,
    нормированное в [0, 1]. Честная оценка (квантили ансамбля) — Фаза 4."""
    return abs(wp - 0.5) * 2.0


class MLService(services_pb2_grpc.MLServiceServicer):
    def __init__(self, model: WinProbability):
        self.model = model

    def Predict(self, request, context):
        if request.model_name not in ("", "win_probability"):
            context.abort(grpc.StatusCode.NOT_FOUND,
                          f"model {request.model_name!r} is not served yet")
        try:
            X = _vector_from_features(request.features)
        except KeyError as missing:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          f"missing features: {missing}")
        wp = float(self.model.predict(X)[0])
        return services_pb2.PredictResponse(
            win_probability_radiant=wp,
            model_version=self.model.version,
        )

    def PredictStream(self, request_iterator, context):
        for frame in request_iterator:
            try:
                X = _vector_from_features(frame.features)
            except KeyError as missing:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                              f"frame t={frame.game_time}: missing features: "
                              f"{missing}")
            wp = float(self.model.predict(X)[0])
            yield services_pb2.WinProbability(
                game_time=frame.game_time,
                radiant=wp,
                confidence=_confidence(wp),
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
    # SO_REUSEPORT выключен: gRPC по умолчанию позволяет НЕСКОЛЬКИМ
    # процессам слушать один порт, и ядро молча балансирует соединения
    # между ними — задвоенный сервер со старой моделью отдавал бы часть
    # ответов незаметно. Пусть второй запуск падает с "address in use".
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         options=[("grpc.so_reuseport", 0)])
    services_pb2_grpc.add_MLServiceServicer_to_server(MLService(model), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("GRPC_PORT", "50051")))
    ap.add_argument("--model", default=os.getenv("MODEL_PATH", str(DEFAULT_MODEL)))
    args = ap.parse_args()

    server, port = build_server(args.model, args.port)
    server.start()
    logger.info("ml-service gRPC listening on :%d (model %s)", port, args.model)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
