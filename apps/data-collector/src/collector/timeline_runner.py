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

from .rawstore import RawMatchStore
from .signals import draft_row, event_rows

logger = logging.getLogger("collector.timeline")

FEATURE_VERSION = "opendota-json@3"

# Источник может объявить свою версию фич (`feature_version`) — у STRATZ
# другой набор заполненных колонок, и сваливать его в одну строку с
# opendota-json значило бы потерять возможность разделить их в анализе.
def _feature_version(source) -> str:
    return getattr(source, "feature_version", None) or FEATURE_VERSION

MTF_COLUMNS = ["match_id", "game_time", "networth_diff", "networth_total",
               "xp_diff",
               "kills_radiant", "kills_dire", "position_advance",
               "alive_diff", "towers_diff", "rax_diff",
               "radiant_win", "tier", "avg_rank", "patch", "feature_version",
               # трек F: объективы, предметы, вижн, нейтралки
               "roshan_diff", "aegis_alive", "buybacks_diff", "first_blood",
               "buyback_availability",
               "item_value_diff", "key_items_diff", "unspent_gold_diff",
               "obs_wards_diff", "vision_coverage_diff",
               "sen_wards_diff", "runes_diff", "neutral_tier_diff",
               "levels_diff"]

# Фичи трека F и волны 1, которых может не быть (битый JSON) — пишем nan.
# Срез считается от конца: добавляя колонку в хвост MTF_COLUMNS, поправить
# и это число, иначе новая фича не попадёт в заполнение nan-ами.
F_TRACK_COLUMNS = MTF_COLUMNS[-14:]

DRAFT_COLUMNS = ["match_id", "patch", "tier", "radiant_win", "radiant_heroes",
                 "dire_heroes", "bans", "first_pick_team", "source"]
EVENT_COLUMNS = ["match_id", "game_time", "kind", "team", "player_slot",
                 "subtype", "x", "y"]


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
        # Хранилище сырого JSON (трек F): выключается RAW_MATCH_STORE=0,
        # недоступный S3 не мешает сбору — только предупреждение.
        self._raw_store = RawMatchStore.from_env()

    def close(self) -> None:
        self._db.close()

    def _ensure_db(self) -> None:
        """Пересоздать мёртвое PG-соединение (рестарт контейнера/Docker
        Desktop): без этого раннер вечно падал бы на первом же запросе
        цикла до ручного перезапуска процесса (инцидент 2026-07-20)."""
        if not self._db.closed:
            try:
                with self._db.cursor() as cur:
                    cur.execute("SELECT 1")
                return
            except psycopg.OperationalError:
                try:
                    self._db.close()
                except Exception:  # noqa: BLE001
                    pass
        logger.warning("postgres: соединение умерло — переподключаюсь")
        self._db = psycopg.connect(self._cfg.postgres_dsn, autocommit=True)

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
                (match_id, self._source.name, f"json:{self._source.name}"))
            cur.execute(
                """INSERT INTO CollectorCursor (source_name, cursor_value, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (source_name)
                   DO UPDATE SET cursor_value = EXCLUDED.cursor_value,
                                 updated_at = NOW()""",
                (self._source.name, cursor_value))

    # -- ClickHouse -----------------------------------------------------------

    def _insert_rows(self, rows: list[dict], tier: str,
                     patch: int = 0, avg_rank: int = 0) -> None:
        def fmt(v) -> str:
            if isinstance(v, float) and math.isnan(v):
                return "nan"
            return str(v)

        lines = []
        for r in rows:
            full = {c: float("nan") for c in F_TRACK_COLUMNS}
            full.update({**r, "tier": tier, "patch": patch,
                         "avg_rank": avg_rank,
                         "feature_version": _feature_version(self._source)})
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

    def _ch_insert(self, table: str, columns: list[str],
                   rows: list[dict]) -> None:
        """Вставка в ClickHouse в TabSeparated. Массивы (Array(String))
        сериализуются в литерал ['a','b'] — формат понимает их сам."""
        if not rows:
            return

        def fmt(v) -> str:
            if isinstance(v, float) and math.isnan(v):
                return "nan"
            if isinstance(v, list):
                return "[" + ",".join("'" + str(x).replace("'", "\\'") + "'"
                                      for x in v) + "]"
            return str(v)

        lines = ["\t".join(fmt(r.get(c, "")) for c in columns) for r in rows]
        query = f"INSERT INTO {table} ({', '.join(columns)}) FORMAT TabSeparated"
        resp = requests.post(
            self._cfg.clickhouse_url,
            params={"database": self._cfg.clickhouse_db, "query": query},
            data=("\n".join(lines) + "\n").encode(),
            headers={"X-ClickHouse-User": self._cfg.clickhouse_user,
                     "X-ClickHouse-Key": self._cfg.clickhouse_password},
            timeout=60)
        resp.raise_for_status()

    def _store_signals(self, tm) -> None:
        """Трек F: драфт, события и сырой JSON матча. Сбой здесь НЕ должен
        рушить сбор — витрина уже записана и она первична."""
        raw = getattr(tm, "raw", None) or {}
        if not raw:
            return
        try:
            d = draft_row(raw)
            if d:
                d["tier"] = tm.tier
                self._ch_insert("MatchDraft", DRAFT_COLUMNS, [d])
            evs = event_rows(raw)
            if evs:
                self._ch_insert("MatchEvents", EVENT_COLUMNS, evs)
        except Exception:  # noqa: BLE001
            logger.warning("матч %d: сигналы не записаны", tm.match_id,
                           exc_info=True)
        # Сырой JSON — страховка на будущее: любая новая фича бэкфиллится
        # без единого вызова API (квота — главный дефицит проекта).
        if self._raw_store is not None:
            try:
                self._raw_store.put(tm.match_id, raw)
            except Exception:  # noqa: BLE001
                logger.warning("матч %d: сырой JSON не сохранён", tm.match_id,
                               exc_info=True)

    # -- цикл -----------------------------------------------------------------

    def collect_once(self) -> int:
        self._ensure_db()
        processed = 0
        for tm in self._source.fetch_new(skip=self._is_collected):
            self._insert_rows(tm.rows, tm.tier, patch=tm.patch,
                              avg_rank=getattr(tm, "avg_rank", 0))
            self._store_signals(tm)
            self._mark_collected(tm.match_id, tm.source_cursor)
            processed += 1
            logger.info("таймлайн матча %d: %d строк (tier=%s)",
                        tm.match_id, len(tm.rows), tm.tier)
        return processed
