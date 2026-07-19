"""gRPC-сервер SimilarityService (Гл. 3: Similarity Engine).

Реализует контракт proto/services.proto:
- FindSimilar(entity="match", reference_id, top_k) → похожие матчи;
  entity player|situation — UNIMPLEMENTED (следующие фазы);
- RetrieveContext(query_embedding, top_k) → текстовые документы ближайших
  матчей (фундамент RAG для LLM-Coach).

Индекс строится из витрин ClickHouse при старте и обновляется фоновым
циклом раз в REFRESH_INTERVAL_S.

Запуск: python -m app  (GRPC_PORT=50052, METRICS_PORT=9111, CLICKHOUSE_*)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent import futures

import grpc
from prometheus_client import Counter, Gauge, start_http_server

from engine.index import MatchIndex
from gen import services_pb2, services_pb2_grpc

logger = logging.getLogger("similarity")

QUERIES = Counter("similarity_queries_total", "Запросы поиска", ["rpc"])
INDEX_SIZE = Gauge("similarity_index_matches", "Матчей в индексе")


class SimilarityService(services_pb2_grpc.SimilarityServiceServicer):
    def __init__(self, index: MatchIndex):
        self.index = index

    def FindSimilar(self, request, context):
        entity = request.entity or "match"
        if entity != "match":
            context.abort(grpc.StatusCode.UNIMPLEMENTED,
                          f"entity {entity!r}: пока поддерживается только 'match'")
        top_k = int(request.top_k) or 5
        try:
            hits = self.index.find_similar(int(request.reference_id), top_k)
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          f"матч {request.reference_id} не в индексе")
        QUERIES.labels("find_similar").inc()
        return services_pb2.SimilarityResult(
            hits=[services_pb2.SimilarHit(id=mid, score=score)
                  for mid, score in hits])

    def RetrieveContext(self, request, context):
        if not request.query_embedding:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          "query_embedding пуст")
        top_k = int(request.top_k) or 3
        try:
            pairs = self.index.retrieve_context(list(request.query_embedding),
                                                top_k)
        except ValueError as e:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        QUERIES.labels("retrieve_context").inc()
        return services_pb2.ContextResult(
            documents=[d for d, _ in pairs],
            scores=[s for _, s in pairs])


def build_server(index: MatchIndex, port: int) -> tuple[grpc.Server, int]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         options=[("grpc.so_reuseport", 0)])
    services_pb2_grpc.add_SimilarityServiceServicer_to_server(
        SimilarityService(index), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"similarity","msg":"%(message)s"}')
    index = MatchIndex(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    n = index.refresh()
    INDEX_SIZE.set(n)

    refresh_s = int(os.getenv("REFRESH_INTERVAL_S", "300"))

    def _refresher():
        while True:
            time.sleep(refresh_s)
            try:
                INDEX_SIZE.set(index.refresh())
            except Exception:  # noqa: BLE001 — живём при сбоях CH
                logger.exception("обновление индекса упало; повтор через цикл")

    threading.Thread(target=_refresher, daemon=True).start()

    metrics_port = int(os.getenv("METRICS_PORT", "9111"))
    if metrics_port:
        start_http_server(metrics_port)
    port = int(os.getenv("GRPC_PORT", "50052"))
    server, bound = build_server(index, port)
    server.start()
    logger.info("similarity gRPC на :%d (индекс: %d матчей, метрики :%d)",
                bound, n, metrics_port)
    server.wait_for_termination()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
