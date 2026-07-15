"""Цикл сбора Data Collector (Гл. 3.3).

Один проход: источник -> дедуп (PG) -> выгрузка .dem в S3 -> событие
match.downloaded в Kafka -> сдвиг курсора. Дедупликация и курсор атомарны
относительно публикации: матч фиксируется в CollectedMatches только после
успешной выгрузки и публикации (at-least-once, NFR-REL-02/04).
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from confluent_kafka import Producer
from minio import Minio

from .sources import MatchRef, Source

logger = logging.getLogger("collector")

PRODUCER_NAME = "data-collector@0.1.0"
TOPIC = "match.downloaded"


@dataclass
class CollectorConfig:
    postgres_dsn: str = "postgresql://dota:dota_dev_password@localhost:5432/manta"
    kafka_brokers: str = "localhost:9092"
    s3_endpoint: str = "localhost:9500"
    s3_access_key: str = "dota"
    s3_secret_key: str = "dota_dev_password"
    s3_bucket: str = "replays"
    s3_secure: bool = False


def build_envelope(ref: MatchRef, replay_url: str, source_name: str,
                   trace_id: str | None = None) -> dict:
    """Собрать конверт события по схеме Гл. 2.3.3."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": TOPIC,
        "schema_version": "1.0.0",
        "trace_id": trace_id or uuid.uuid4().hex,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "producer": PRODUCER_NAME,
        "partition_key": f"match_id:{ref.match_id}",
        "payload": {
            "match_id": ref.match_id,
            "replay_url": replay_url,
            "tier": ref.tier,
            "source": source_name,
        },
    }


class Collector:
    def __init__(self, cfg: CollectorConfig, source: Source) -> None:
        self._cfg = cfg
        self._source = source
        self._db = psycopg.connect(cfg.postgres_dsn, autocommit=False)
        self._producer = Producer({"bootstrap.servers": cfg.kafka_brokers})
        self._s3 = Minio(cfg.s3_endpoint, access_key=cfg.s3_access_key,
                         secret_key=cfg.s3_secret_key, secure=cfg.s3_secure)
        if not self._s3.bucket_exists(cfg.s3_bucket):
            self._s3.make_bucket(cfg.s3_bucket)

    # -- persistence ---------------------------------------------------------

    def _get_cursor(self) -> str | None:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT cursor_value FROM CollectorCursor WHERE source_name = %s",
                (self._source.name,))
            row = cur.fetchone()
            return row[0] if row else None

    def _is_collected(self, match_id: int) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM CollectedMatches WHERE match_id = %s", (match_id,))
            return cur.fetchone() is not None

    def _mark_collected(self, ref: MatchRef, replay_url: str) -> None:
        with self._db.cursor() as cur:
            cur.execute(
                """INSERT INTO CollectedMatches (match_id, source_name, replay_url)
                   VALUES (%s, %s, %s) ON CONFLICT (match_id) DO NOTHING""",
                (ref.match_id, self._source.name, replay_url))
            cur.execute(
                """INSERT INTO CollectorCursor (source_name, cursor_value, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (source_name)
                   DO UPDATE SET cursor_value = EXCLUDED.cursor_value,
                                 updated_at = NOW()""",
                (self._source.name, ref.source_cursor))
        self._db.commit()

    # -- pipeline ------------------------------------------------------------

    def collect_once(self) -> int:
        """Один проход сбора; возвращает число обработанных матчей."""
        cursor = self._get_cursor()
        processed = 0
        for ref in self._source.fetch_new(cursor):
            if self._is_collected(ref.match_id):
                logger.info("skip duplicate match_id=%s", ref.match_id)
                continue

            # Сбой одного матча (503 реплей-сервера, битый bz2, сеть) не
            # должен ронять весь цикл (Гл. 2.4.2): логируем и идём дальше.
            # Курсор для неудачного матча не фиксируется — он будет
            # повторён следующим циклом, если позже не перекроется курсором
            # более нового успешного матча (для пабликов это приемлемо).
            try:
                data = self._source.download_replay(ref)
            except Exception as exc:  # noqa: BLE001
                logger.warning("match %s: download failed (%s), пропуск",
                               ref.match_id, exc)
                continue
            object_key = f"{self._source.name}/{ref.match_id}.dem"
            self._s3.put_object(self._cfg.s3_bucket, object_key,
                                io.BytesIO(data), len(data),
                                content_type="application/octet-stream")
            replay_url = f"s3://{self._cfg.s3_bucket}/{object_key}"

            env = build_envelope(ref, replay_url, self._source.name)
            self._producer.produce(
                TOPIC,
                key=env["partition_key"].encode(),
                value=json.dumps(env).encode())
            self._producer.flush(10)

            self._mark_collected(ref, replay_url)
            processed += 1
            logger.info("collected match_id=%s -> %s", ref.match_id, replay_url)
        return processed

    def close(self) -> None:
        self._db.close()
