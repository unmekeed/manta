"""gRPC-сервер RecommendationService — AI Coach (Гл. 3, Гл. 6.3).

BuildPlan(player_id) → TrainingPlan: приоритизированные навыки для
тренировки, выведенные из MatchReports игрока (ошибки с SHAP-вкладами,
Safety Index, laning/impact) + прецеденты похожих матчей через
Similarity.RetrieveContext (RAG). Полный текст плана кладётся в
resource_url первого элемента с префиксом text: — прото-контракт
(источник истины) не меняем, а фронту нужен именно текст.

LLM-слой опционален (COACH_LLM_PROVIDER/KEY); без него — шаблонный план.

Запуск: python -m serve_coach  (GRPC_PORT=50054, METRICS_PORT=9113,
POSTGRES_DSN, SIMILARITY_ADDR=localhost:50052)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent import futures

import grpc
import psycopg
from prometheus_client import Counter, start_http_server

from engine.llm import llm_from_env
from engine.plan import analyze_player, render_plan
from gen import services_pb2, services_pb2_grpc

logger = logging.getLogger("coach")

PLANS = Counter("coach_plans_total", "Построенные планы", ["mode"])


class CoachService(services_pb2_grpc.RecommendationServiceServicer):
    def __init__(self, pg_dsn: str, similarity_addr: str):
        self._pg_dsn = pg_dsn
        self._sim_addr = similarity_addr
        self._llm = llm_from_env()
        self._lock = threading.Lock()
        self._db = None

    def _conn(self):
        with self._lock:
            if self._db is None or self._db.closed:
                self._db = psycopg.connect(self._pg_dsn, autocommit=True)
            return self._db

    # -- источники ------------------------------------------------------------

    def _reports_for_player(self, player_id: int, limit: int = 50) -> list[dict]:
        """Отчёты, где участвует игрок (analysis.players[].player_id)."""
        rows = self._conn().execute(
            """SELECT match_id, analysis FROM MatchReports
               ORDER BY generated_at DESC LIMIT %s""", (limit,)).fetchall()
        out = []
        for mid, analysis in rows:
            a = analysis if isinstance(analysis, dict) else json.loads(analysis)
            if any(int(p.get("player_id", -1)) == player_id
                   for p in a.get("players") or []):
                out.append({"match_id": mid, "analysis": a})
        return out

    def _similar_context(self, match_ids: list[int]) -> list[str]:
        """Прецеденты через Similarity (RAG); сбой похожести не валит план."""
        if not match_ids:
            return []
        try:
            chan = grpc.insecure_channel(self._sim_addr)
            stub = services_pb2_grpc.SimilarityServiceStub(chan)
            res = stub.FindSimilar(services_pb2.SimilarityQuery(
                entity="match", reference_id=match_ids[0], top_k=3),
                timeout=5)
            docs = []
            for hit in res.hits:
                docs.append(f"матч {hit.id} (похожесть {hit.score:.2f})")
            return docs
        except grpc.RpcError as e:
            logger.warning("similarity недоступен: %s — план без прецедентов",
                           e.code())
            return []

    # -- RPC ------------------------------------------------------------------

    def BuildPlan(self, request, context):
        player_id = int(request.player_id)
        reports = self._reports_for_player(player_id)
        obs = analyze_player(reports, player_id)
        if not obs:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          f"нет отчётов с игроком {player_id}")
        ctx_docs = self._similar_context(
            [r["match_id"] for r in reports[:1]])
        text = render_plan(obs, ctx_docs, player_id)
        final_text = self._llm.generate(text)
        PLANS.labels(self._llm.name).inc()

        items = [services_pb2.PlanItem(skill=o.skill, priority=o.priority,
                                       resource_url=o.resource_url)
                 for o in obs]
        if items:
            items[0].resource_url = "text:" + final_text
        return services_pb2.TrainingPlan(items=items)


def build_server(pg_dsn: str, similarity_addr: str, port: int
                 ) -> tuple[grpc.Server, int, CoachService]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         options=[("grpc.so_reuseport", 0)])
    svc = CoachService(pg_dsn, similarity_addr)
    services_pb2_grpc.add_RecommendationServiceServicer_to_server(svc, server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound, svc


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"coach","msg":"%(message)s"}')
    metrics_port = int(os.getenv("METRICS_PORT", "9113"))
    if metrics_port:
        start_http_server(metrics_port)
    port = int(os.getenv("GRPC_PORT", "50054"))
    server, bound, _ = build_server(
        os.getenv("POSTGRES_DSN",
                  "postgresql://dota:dota_dev_password@localhost:5432/manta"),
        os.getenv("SIMILARITY_ADDR", "localhost:50052"),
        port)
    server.start()
    logger.info("coach gRPC на :%d (метрики :%d)", bound, metrics_port)
    server.wait_for_termination()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
