"""Петля Feature Extractor (Гл. 3.5/3.6, спринт 9).

replay.parsed → чтение сырых таблиц ClickHouse → расчёт фич →
PlayerMatchFeatures + MatchTimelineFeatures → features.calculated.

Идемпотентность: витрины на ReplacingMergeTree(computed_at) — повторная
обработка того же матча замещает строки, at-least-once безопасен.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from confluent_kafka import Consumer, Producer

from .clickhouse import ClickHouse
from .features import FEATURE_VERSION, Roster, player_features, timeline_features

logger = logging.getLogger("extractor")

PRODUCER_NAME = "feature-extractor@0.1.0"
TOPIC_IN = "replay.parsed"
TOPIC_OUT = "features.calculated"


@dataclass
class ExtractorConfig:
    kafka_brokers: str = field(
        default_factory=lambda: os.getenv("KAFKA_BROKERS", "localhost:9092"))
    group_id: str = field(
        default_factory=lambda: os.getenv("KAFKA_GROUP_ID", "feature-extractor"))
    clickhouse_url: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    clickhouse_db: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_DB", "dota_analyst"))
    clickhouse_user: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_USER", "dota"))
    clickhouse_password: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))


def build_envelope(match_id: int, payload: dict, trace_id: str | None) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": TOPIC_OUT,
        "schema_version": "1.0.0",
        "trace_id": trace_id or uuid.uuid4().hex,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "producer": PRODUCER_NAME,
        "partition_key": f"match_id:{match_id}",
        "payload": payload,
    }


class Extractor:
    def __init__(self, cfg: ExtractorConfig):
        self.cfg = cfg
        self.ch = ClickHouse(cfg.clickhouse_url, cfg.clickhouse_db,
                             cfg.clickhouse_user, cfg.clickhouse_password)
        self.producer = Producer({"bootstrap.servers": cfg.kafka_brokers})

    # -- обработка одного матча ------------------------------------------------

    def process_match(self, match_id: int, players: list[dict], winner: str,
                      duration_s: float, trace_id: str | None,
                      tier: str = "") -> dict:
        roster = Roster.from_players(players, winner)

        economy = self.ch.select(
            "SELECT player_id, game_time, net_worth, total_gold, total_xp,"
            "       lh, dn"
            "  FROM EconomyTimeline WHERE match_id = {match_id:UInt64}"
            " ORDER BY player_id, game_time",
            {"match_id": match_id})
        kills = self.ch.select(
            "SELECT game_time, target FROM ReplayEvents"
            " WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "   AND target LIKE 'npc_dota_hero_%'"
            " ORDER BY game_time",
            {"match_id": match_id})
        positions = self.ch.select(
            "SELECT game_time, hero, x, y FROM PositionSnapshots"
            " WHERE match_id = {match_id:UInt64} ORDER BY game_time",
            {"match_id": match_id})
        if not economy:
            raise ValueError(f"no economy rows for match {match_id}")

        prows = player_features(economy, roster, duration_s,
                                positions=positions)
        trows = timeline_features(economy, kills, roster, positions=positions)
        for r in prows:
            r["match_id"] = match_id
            r["tier"] = tier
        for r in trows:
            r["match_id"] = match_id
            r["tier"] = tier

        self.ch.insert_rows("PlayerMatchFeatures", prows)
        self.ch.insert_rows("MatchTimelineFeatures", trows)

        payload = {
            "match_id": match_id,
            "feature_version": FEATURE_VERSION,
            "player_rows": len(prows),
            "timeline_rows": len(trows),
        }
        env = build_envelope(match_id, payload, trace_id)
        self.producer.produce(TOPIC_OUT, key=env["partition_key"],
                              value=json.dumps(env).encode("utf-8"))
        self.producer.flush(10)
        logger.info("features calculated: match=%s players=%d timeline=%d",
                    match_id, len(prows), len(trows))
        return payload

    # -- бэкфилл ---------------------------------------------------------------

    def backfill(self, match_ids: list[int] | None = None) -> int:
        """Пересчитать фичи существующих матчей (новая версия фич).

        Ростер восстанавливается из PlayerMatchFeatures; исход — из won.
        Витрины на ReplacingMergeTree — пересчёт замещает строки.
        """
        if match_ids is None:
            rows = self.ch.select(
                "SELECT DISTINCT match_id FROM MatchTimelineFeatures"
                " ORDER BY match_id")
            match_ids = [int(r["match_id"]) for r in rows]
        done = 0
        for mid in match_ids:
            # Любой сбой одного матча (в т.ч. транзиентный 503 ClickHouse
            # на подготовительных запросах) не прерывает весь бэкфилл.
            try:
                prows = self.ch.select(
                    "SELECT player_id, team, hero, player_name, won, duration_s"
                    "  FROM PlayerMatchFeatures FINAL"
                    " WHERE match_id = {match_id:UInt64} ORDER BY player_id",
                    {"match_id": mid})
                if not prows:
                    logger.warning("match %s: нет PlayerMatchFeatures, пропуск",
                                   mid)
                    continue
                players = [{"team": int(r["team"]), "name": r["player_name"],
                            "hero": r["hero"]} for r in prows]
                won_teams = {int(r["team"]) for r in prows if int(r["won"]) == 1}
                winner = "Radiant" if won_teams == {2} else "Dire"
                duration = float(prows[0].get("duration_s", 0))
                tier_rows = self.ch.select(
                    "SELECT any(tier) AS tier FROM MatchTimelineFeatures"
                    " WHERE match_id = {match_id:UInt64}", {"match_id": mid})
                tier = str(tier_rows[0]["tier"]) if tier_rows else ""
                self.process_match(mid, players, winner, duration,
                                   trace_id=None, tier=tier)
                done += 1
            except Exception:  # noqa: BLE001
                logger.exception("backfill failed for match %s", mid)
        logger.info("backfill done: %d/%d matches", done, len(match_ids))
        return done

    # -- Kafka-петля -----------------------------------------------------------

    def run(self) -> None:
        consumer = Consumer({
            "bootstrap.servers": self.cfg.kafka_brokers,
            "group.id": self.cfg.group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            # Агрессивные таймауты: зависшее после сна/рестарта брокера
            # соединение должно рваться и пересоздаваться, а не молчать.
            "socket.keepalive.enable": True,
            "session.timeout.ms": 15000,
            "reconnect.backoff.max.ms": 5000,
        })
        consumer.subscribe([TOPIC_IN])
        logger.info("feature-extractor started: brokers=%s topic=%s",
                    self.cfg.kafka_brokers, TOPIC_IN)
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
            players = payload.get("players") or []
            winner = payload.get("winner", "")
            duration_s = float(payload.get("duration_s", 0))
            tier = str(payload.get("tier", "") or "")
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("bad replay.parsed event, skipping: %s", exc)
            return
        if not players:
            # Событие старой схемы (без ростера) — фичи посчитать нельзя.
            logger.warning("match %s: no roster in event, skipping", match_id)
            return
        try:
            self.process_match(match_id, players, winner, duration_s,
                               env.get("trace_id"), tier=tier)
        except Exception:  # noqa: BLE001 — логируем и не блокируем партицию
            logger.exception("feature extraction failed for match %s", match_id)
