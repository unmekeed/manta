"""gRPC-сервер FeatureStore (Гл. 3.6): онлайн-фичи поверх Redis.

- WriteFeatures(FeatureBatch): векторы батча кладутся в view батча;
  сущность каждого вектора — match_id (+player_id) внутри values.
- GetOnlineFeatures(FeatureRequest): feature_refs вида «view:feature»,
  entity_keys — идентификатор сущности. Пустой результат → NOT_FOUND.

Запуск: python -m serve_features  (GRPC_PORT=50055, METRICS_PORT=9114,
REDIS_URL=redis://localhost:6379/0, FS_TTL_S=604800)
"""
from __future__ import annotations

import logging
import os
from concurrent import futures

import grpc
import redis
from prometheus_client import Counter, start_http_server

from gen import services_pb2, services_pb2_grpc
from store.redis_store import RedisFeatureStore

logger = logging.getLogger("feature-store")

WRITES = Counter("fs_vectors_written_total", "Принятые векторы фич")
READS = Counter("fs_reads_total", "Запросы GetOnlineFeatures", ["outcome"])


class FeatureStoreServicer(services_pb2_grpc.FeatureStoreServicer):
    def __init__(self, store: RedisFeatureStore):
        self._store = store

    def WriteFeatures(self, request, context):
        vectors = [dict(v.values) for v in request.vectors]
        view = request.feature_view or "default"
        ts = None
        if request.vectors and request.vectors[0].HasField("event_timestamp"):
            ts = request.vectors[0].event_timestamp.ToDatetime().timestamp()
        written = self._store.write(view, vectors, ts=ts)
        WRITES.inc(written)
        return services_pb2.WriteAck(written=written, ok=True)

    def GetOnlineFeatures(self, request, context):
        if not request.feature_refs or not request.entity_keys:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          "нужны feature_refs (view:feature) и entity_keys")
        values, ts = self._store.read(list(request.feature_refs),
                                      dict(request.entity_keys))
        if not values:
            READS.labels("miss").inc()
            context.abort(grpc.StatusCode.NOT_FOUND,
                          "фичи не найдены (сущность вне онлайн-слоя или TTL истёк)")
        READS.labels("hit").inc()
        resp = services_pb2.FeatureVector()
        for k, v in values.items():
            resp.values[k] = v
        if ts:
            resp.event_timestamp.FromSeconds(int(ts))
        return resp


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"feature-store","msg":"%(message)s"}')
    client = redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    client.ping()
    store = RedisFeatureStore(client,
                              ttl_s=int(os.getenv("FS_TTL_S", str(7 * 24 * 3600))))

    metrics_port = int(os.getenv("METRICS_PORT", "9114"))
    if metrics_port:
        start_http_server(metrics_port)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    services_pb2_grpc.add_FeatureStoreServicer_to_server(
        FeatureStoreServicer(store), server)
    port = int(os.getenv("GRPC_PORT", "50055"))
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info("feature-store gRPC на :%d (метрики :%d)", port, metrics_port)
    server.wait_for_termination()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
