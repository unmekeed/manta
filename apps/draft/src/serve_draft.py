"""gRPC-сервер DraftService (Гл. 3: Draft Engine, бейзлайн частот).

SimulateDraft(DraftState) → DraftRecommendation: предсказанный винрейт
Radiant текущего драфта + топ-5 пиков для действующей стороны с
ожидаемым винрейтом и причиной. GNN из спеки — при 10^4+ матчей; здесь
честный частотный бейзлайн со сглаживанием (см. engine/stats.py).

Статистика строится из PlayerMatchFeatures (только реплей-матчи — у
JSON-пути нет составов) и обновляется фоном раз в REFRESH_INTERVAL_S.

Запуск: python -m serve_draft  (GRPC_PORT=50053, METRICS_PORT=9112)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent import futures
from pathlib import Path

import grpc

import manta_grpc
import requests
from manta_data import load_json
from prometheus_client import Counter, Gauge

from engine.recommend import suggest
from engine.stats import DraftStats, build_stats
from gen import services_pb2, services_pb2_grpc

logger = logging.getLogger("draft")

QUERIES = Counter("draft_queries_total", "Запросы SimulateDraft")
STATS_MATCHES = Gauge("draft_stats_matches", "Матчей в статистике драфта")


def _load_hero_dict() -> dict[str, int]:
    """npc-имя → Valve hero id из libs/data/heroes.json.

    Путь не считается шагами вверх (`parents[3]` для монорепо, `parents[1]`
    для образа): такой перебор ронял коллекторы и report-generator на VPS —
    в чужой раскладке нужного шага просто нет. Резолвер — libs/manta_data.py.
    """
    override = os.getenv("HEROES_PATH")
    raw = {}
    if override:
        try:
            raw = json.loads(Path(override).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    if not raw:
        raw = load_json("heroes.json", {})
    if not raw:
        logger.warning("heroes.json не найден — статистика будет пустой")
    return {name: int(v.get("id", 0)) for name, v in raw.items()}


class StatsHolder:
    def __init__(self, ch: tuple[str, str, str, str], hero_ids: dict[str, int]):
        self._ch = ch
        self._hero_ids = hero_ids
        self.stats = DraftStats()

    def refresh(self) -> int:
        url, db, user, pwd = self._ch
        resp = requests.post(
            url, params={"database": db, "default_format": "JSONEachRow"},
            data="SELECT match_id, team, hero, won FROM PlayerMatchFeatures"
                 " FINAL",
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": pwd},
            timeout=180)
        resp.raise_for_status()
        rows = [json.loads(line) for line in resp.text.splitlines() if line]
        self.stats = build_stats(rows, lambda n: self._hero_ids.get(n, 0))
        logger.info("статистика драфта: %d матчей, %d героев",
                    self.stats.n_matches, len(self.stats.solo))
        return self.stats.n_matches


class DraftService(services_pb2_grpc.DraftServiceServicer):
    def __init__(self, holder: StatsHolder):
        self.holder = holder

    def SimulateDraft(self, request, context):
        stats = self.holder.stats
        if stats.n_matches == 0:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION,
                          "статистика драфта пуста (нет матчей с составами)")
        wr, suggestions, _side = suggest(
            stats,
            [int(h) for h in request.radiant_picks],
            [int(h) for h in request.dire_picks],
            [int(h) for h in request.bans],
            request.next_action)
        QUERIES.inc()
        return services_pb2.DraftRecommendation(
            predicted_winrate_radiant=wr,
            suggestions=[
                services_pb2.HeroSuggestion(
                    hero_id=s["hero_id"],
                    expected_winrate=s["expected_winrate"],
                    reason=s["reason"])
                for s in suggestions])


def build_server(holder: StatsHolder, port: int) -> tuple[grpc.Server, int]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                         options=[("grpc.so_reuseport", 0)])
    services_pb2_grpc.add_DraftServiceServicer_to_server(
        DraftService(holder), server)
    bound = manta_grpc.add_port(server, port, "draft")
    return server, bound


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"draft","msg":"%(message)s"}')
    holder = StatsHolder(
        (os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
         os.getenv("CLICKHOUSE_DB", "manta"),
         os.getenv("CLICKHOUSE_USER", "dota"),
         os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password")),
        _load_hero_dict())
    STATS_MATCHES.set(holder.refresh())

    refresh_s = int(os.getenv("REFRESH_INTERVAL_S", "600"))

    def _refresher():
        while True:
            time.sleep(refresh_s)
            try:
                STATS_MATCHES.set(holder.refresh())
            except Exception:  # noqa: BLE001
                logger.exception("обновление статистики упало; повтор через цикл")

    threading.Thread(target=_refresher, daemon=True).start()

    metrics_port = int(os.getenv("METRICS_PORT", "9112"))
    if metrics_port:
        manta_grpc.serve_metrics(metrics_port, "draft")
    port = int(os.getenv("GRPC_PORT", "50053"))
    server, bound = build_server(holder, port)
    server.start()
    logger.info("draft gRPC на :%d (метрики :%d)", bound, metrics_port)
    server.wait_for_termination()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
