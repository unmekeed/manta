"""Раннер JSON-таймлайн источника: OpenDota → MatchTimelineFeatures напрямую.

В отличие от реплей-пути (S3 → Kafka → парсер → экстрактор) здесь конвейер
короткий: источник отдаёт готовые строки витрины, раннер пишет их в
ClickHouse и помечает матч в CollectedMatches (общий дедуп с реплей-путём:
один матч никогда не въезжает дважды, каким бы путём ни пришёл).

События features.calculated НЕ публикуются: у JSON-матчей нет ReplayEvents/
позиций, полноценный отчёт по ним не собрать — они существуют ради датасета
Win Probability, который читает витрину напрямую.

Вставка в ClickHouse — TabSeparated: текстовые nan корректно парсятся в
Float64 (JSONEachRow с null для не-Nullable колонки не прошёл бы).
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

import psycopg
import requests

logger = logging.getLogger("collector.timeline")

FEATURE_VERSION = "opendota-json@3"

MTF_COLUMNS = ["match_id", "game_time", "networth_diff", "networth_total",
               "xp_diff",
               "kills_radiant", "kills_dire", "position_advance",
               "alive_diff", "towers_diff", "rax_diff",
               "radiant_win", "tier", "feature_version"]


@dataclass
class TimelineConfig:
    postgres_dsn: str = field(default_factory=lambda: os.getenv(
        "POSTGRES_DSN",
        "postgresql://dota:dota_dev_password@localhost:5432/manta"))
    clickhouse_url: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    clickhouse_db: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_DB", "manta"))
    clickhouse_user: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_USER", "dota"))
    clickhouse_password: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))


class TimelineCollector:
    def __init__(self, cfg: TimelineConfig, source) -> None:
        self._cfg = cfg
        self._source = source
        self._db = psycopg.connect(cfg.postgres_dsn, autocommit=True)

    def close(self) -> None:
        self._db.close()

    # -- дедуп (общая таблица с реплей-путём) ---------------------------------

    def _is_collected(self, match_id: int) -> bool:
        with self._db.cursor() as cur:
            cur.execute("SELECT 1 FROM CollectedMatches WHERE match_id = %s",
                        (match_id,))
            return cur.fetchone() is not None

    def _mark_collected(self, match_id: int, cursor_value: str) -> None:
        with self._db.cursor() as cur:
            cur.execute(
                """INSERT INTO CollectedMatches (match_id, source_name, replay_url)
                   VALUES (%s, %s, %s) ON CONFLICT (match_id) DO NOTHING""",
                (match_id, self._source.name, "json:opendota"))
            cur.execute(
                """INSERT INTO CollectorCursor (source_name, cursor_value, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (source_name)
                   DO UPDATE SET cursor_value = EXCLUDED.cursor_value,
                                 updated_at = NOW()""",
                (self._source.name, cursor_value))

    # -- ClickHouse -----------------------------------------------------------

    def _insert_rows(self, rows: list[dict], tier: str) -> None:
        def fmt(v) -> str:
            if isinstance(v, float) and math.isnan(v):
                return "nan"
            return str(v)

        lines = []
        for r in rows:
            full = {**r, "tier": tier, "feature_version": FEATURE_VERSION}
            lines.append("\t".join(fmt(full[c]) for c in MTF_COLUMNS))
        query = (f"INSERT INTO MatchTimelineFeatures ({', '.join(MTF_COLUMNS)}) "
                 f"FORMAT TabSeparated")
        resp = requests.post(
            self._cfg.clickhouse_url,
            params={"database": self._cfg.clickhouse_db, "query": query},
            data=("\n".join(lines) + "\n").encode(),
            headers={"X-ClickHouse-User": self._cfg.clickhouse_user,
                     "X-ClickHouse-Key": self._cfg.clickhouse_password},
            timeout=60)
        resp.raise_for_status()

    # -- цикл -----------------------------------------------------------------

    def collect_once(self) -> int:
        processed = 0
        for tm in self._source.fetch_new(skip=self._is_collected):
            self._insert_rows(tm.rows, tm.tier)
            self._mark_collected(tm.match_id, tm.source_cursor)
            processed += 1
            logger.info("таймлайн матча %d: %d строк (tier=%s)",
                        tm.match_id, len(tm.rows), tm.tier)
        return processed
