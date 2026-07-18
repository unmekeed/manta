"""Петля Report Generator (Гл. 3, спринт 15).

features.calculated → витрины ClickHouse → WP-кривая (gRPC MLService
PredictStream) → MatchReports (PostgreSQL, UPSERT) → report.generated.

Отчёт материализуется при генерации: путь чтения (шлюз) не трогает ни
ClickHouse, ни ML Service. Повторная доставка события перегенерирует
отчёт — UPSERT делает это идемпотентным.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import grpc
import psycopg
import requests
from confluent_kafka import Consumer, Producer
from prometheus_client import Counter, Histogram, start_http_server

from .builder import build_analysis, build_timeline
from .gen import services_pb2, services_pb2_grpc

logger = logging.getLogger("reportgen")

REPORTS_GENERATED = Counter(
    "reports_generated_total", "Сгенерированные отчёты")
REPORTS_FAILED = Counter(
    "reports_failed_total", "Сбои генерации отчётов")
REPORT_DURATION = Histogram(
    "report_duration_seconds", "Время генерации отчёта",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20))

PRODUCER_NAME = "report-generator@0.1.0"
TOPIC_IN = "features.calculated"
TOPIC_OUT = "report.generated"


@dataclass
class ReportgenConfig:
    kafka_brokers: str = field(
        default_factory=lambda: os.getenv("KAFKA_BROKERS", "localhost:9092"))
    group_id: str = field(
        default_factory=lambda: os.getenv("KAFKA_GROUP_ID", "report-generator"))
    clickhouse_url: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    clickhouse_db: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_DB", "manta"))
    clickhouse_user: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_USER", "dota"))
    clickhouse_password: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    postgres_dsn: str = field(default_factory=lambda: os.getenv(
        "POSTGRES_DSN",
        "postgresql://dota:dota_dev_password@localhost:5432/manta"))
    ml_grpc_addr: str = field(
        default_factory=lambda: os.getenv("ML_GRPC_ADDR", "localhost:50051"))


class ReportGenerator:
    def __init__(self, cfg: ReportgenConfig):
        self.cfg = cfg
        self.db = psycopg.connect(cfg.postgres_dsn, autocommit=True)
        self.producer = Producer({"bootstrap.servers": cfg.kafka_brokers})
        self.ml = services_pb2_grpc.MLServiceStub(
            grpc.insecure_channel(cfg.ml_grpc_addr))

    # -- источники данных -------------------------------------------------------

    def _ch_select(self, query: str, match_id: int) -> list[dict]:
        resp = requests.post(
            self.cfg.clickhouse_url,
            params={"database": self.cfg.clickhouse_db,
                    "default_format": "JSONEachRow",
                    "param_match_id": str(match_id)},
            data=query,
            headers={"X-ClickHouse-User": self.cfg.clickhouse_user,
                     "X-ClickHouse-Key": self.cfg.clickhouse_password},
            timeout=60)
        resp.raise_for_status()
        return [json.loads(line) for line in resp.text.splitlines() if line]

    def _timeline_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT game_time, networth_diff, xp_diff, kills_radiant,"
            "       kills_dire, position_advance, radiant_win"
            "  FROM MatchTimelineFeatures FINAL"
            " WHERE match_id = {match_id:UInt64} ORDER BY game_time", match_id)

    def _kill_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT game_time, target, attacker FROM ReplayEvents"
            " WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "   AND target LIKE 'npc_dota_hero_%'"
            " ORDER BY game_time", match_id)

    def _position_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT game_time, hero, x, y FROM PositionSnapshots"
            " WHERE match_id = {match_id:UInt64} ORDER BY game_time", match_id)

    def _player_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT player_id, team, hero, player_name, won, gpm, xpm,"
            "       lh_at_10, dn_at_10, lane, lane_nw_diff_at_10, gold_share"
            "  FROM PlayerMatchFeatures FINAL"
            " WHERE match_id = {match_id:UInt64} ORDER BY player_id", match_id)

    def _wp_curve(self, match_id: int, rows: list[dict]
                  ) -> tuple[list[float], list[list[dict]], str]:
        def _pos(r: dict) -> float:
            # У JSON-матчей позиции нет: ClickHouse отдаёт NaN как null.
            # NaN — корректный пропуск для модели (protobuf double его несёт).
            v = r.get("position_advance")
            return float(v) if v is not None else float("nan")

        def frames():
            for r in rows:
                kills_r = float(r["kills_radiant"])
                kills_d = float(r["kills_dire"])
                yield services_pb2.FeatureFrame(
                    match_id=match_id, game_time=int(r["game_time"]),
                    features=services_pb2.FeatureVector(values={
                        "game_time": float(r["game_time"]),
                        "networth_diff": float(r["networth_diff"]),
                        "xp_diff": float(r["xp_diff"]),
                        "kills_diff": kills_r - kills_d,
                        "kills_total": kills_r + kills_d,
                        "position_advance": _pos(r),
                    }))

        wp, drivers = [], []
        for p in self.ml.PredictStream(frames()):
            wp.append(p.radiant)
            drivers.append([{"feature": c.feature_name,
                             "value": round(c.contribution, 4)}
                            for c in p.top_contributions])
        # Версию модели узнаём отдельным Predict по последней точке.
        last = rows[-1]
        resp = self.ml.Predict(services_pb2.PredictRequest(
            match_id=match_id, model_name="win_probability",
            features=services_pb2.FeatureVector(values={
                "game_time": float(last["game_time"]),
                "networth_diff": float(last["networth_diff"]),
                "xp_diff": float(last["xp_diff"]),
                "kills_diff": float(last["kills_radiant"]) - float(last["kills_dire"]),
                "kills_total": float(last["kills_radiant"]) + float(last["kills_dire"]),
                "position_advance": _pos(last),
            })))
        return wp, drivers, resp.model_version

    # -- генерация ---------------------------------------------------------------

    def generate(self, match_id: int, feature_version: str = "",
                 trace_id: str | None = None) -> dict:
        rows = self._timeline_rows(match_id)
        if not rows:
            raise ValueError(f"no timeline features for match {match_id}")
        players = self._player_rows(match_id)
        winner = "Radiant" if int(rows[-1]["radiant_win"]) == 1 else "Dire"

        wp, drivers, model_version = self._wp_curve(match_id, rows)
        timeline = build_timeline(match_id, rows, wp, drivers)
        kills = self._kill_rows(match_id)
        positions = self._position_rows(match_id)
        analysis = build_analysis(match_id, winner, players, timeline,
                                  model_version, kills=kills,
                                  positions=positions)

        self.db.execute(
            """INSERT INTO MatchReports
                   (match_id, analysis, timeline, model_version,
                    feature_version, generated_at)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON CONFLICT (match_id) DO UPDATE SET
                   analysis = EXCLUDED.analysis,
                   timeline = EXCLUDED.timeline,
                   model_version = EXCLUDED.model_version,
                   feature_version = EXCLUDED.feature_version,
                   generated_at = NOW()""",
            (match_id, json.dumps(analysis, ensure_ascii=False),
             json.dumps(timeline), model_version, feature_version))

        payload = {
            "match_id": match_id,
            "report_version": analysis["report_version"],
            "model_version": model_version,
            "summary": analysis["narrative"],
        }
        env = {
            "event_id": str(uuid.uuid4()),
            "event_type": TOPIC_OUT,
            "schema_version": "1.0.0",
            "trace_id": trace_id or uuid.uuid4().hex,
            "occurred_at": datetime.now(timezone.utc).isoformat()
                                   .replace("+00:00", "Z"),
            "producer": PRODUCER_NAME,
            "partition_key": f"match_id:{match_id}",
            "payload": payload,
        }
        self.producer.produce(TOPIC_OUT, key=env["partition_key"],
                              value=json.dumps(env, ensure_ascii=False).encode())
        self.producer.flush(10)
        logger.info("report generated: match=%s model=%s points=%d",
                    match_id, model_version, len(timeline["points"]))
        return payload

    # -- Kafka-петля ---------------------------------------------------------------

    def run(self) -> None:
        consumer = Consumer({
            "bootstrap.servers": self.cfg.kafka_brokers,
            "group.id": self.cfg.group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "socket.keepalive.enable": True,
            "session.timeout.ms": 15000,
            "reconnect.backoff.max.ms": 5000,
        })
        consumer.subscribe([TOPIC_IN])
        metrics_port = int(os.getenv("METRICS_PORT", "9103"))
        if metrics_port:
            start_http_server(metrics_port)
        logger.info("report-generator started: brokers=%s topic=%s ml=%s metrics=:%s",
                    self.cfg.kafka_brokers, TOPIC_IN, self.cfg.ml_grpc_addr,
                    metrics_port)
        try:
            while True:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    logger.error("kafka error: %s", msg.error())
                    continue
                self._handle(msg.value())
                consumer.commit(msg)
        except KeyboardInterrupt:
            pass
        finally:
            consumer.close()

    def _handle(self, raw: bytes) -> None:
        try:
            env = json.loads(raw)
            payload = env.get("payload", {})
            match_id = int(payload["match_id"])
            feature_version = str(payload.get("feature_version", ""))
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("bad features.calculated event, skipping: %s", exc)
            return
        try:
            with REPORT_DURATION.time():
                self.generate(match_id, feature_version, env.get("trace_id"))
            REPORTS_GENERATED.inc()
        except Exception:  # noqa: BLE001 — не блокируем партицию
            REPORTS_FAILED.inc()
            logger.exception("report generation failed for match %s", match_id)
