"""Точка входа Data Collector: периодический цикл сбора.

Запуск:  python -m collector [--source fixture|opendota] [--interval 300]
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from prometheus_client import Counter, start_http_server

from .runner import Collector, CollectorConfig

MATCHES_COLLECTED = Counter("matches_collected_total",
                            "Собранные и опубликованные матчи")
CYCLES_FAILED = Counter("collector_cycles_failed_total",
                        "Циклы сбора, упавшие по внешним причинам")
from .sources.fixture import FixtureSource
from .sources.opendota import OpenDotaSource
from .sources.opendota_public import OpenDotaPublicSource


def build_source(name: str):
    limit = int(os.getenv("OPENDOTA_LIMIT", "3"))
    if name == "fixture":
        return FixtureSource()
    if name == "opendota":
        return OpenDotaSource(limit_per_cycle=limit)
    if name == "opendota-public":
        min_patch = os.getenv("OPENDOTA_MIN_PATCH")
        return OpenDotaPublicSource(
            limit_per_cycle=limit,
            min_rank=int(os.getenv("OPENDOTA_MIN_RANK", "80")),
            min_patch=int(min_patch) if min_patch else None,
        )
    raise ValueError(f"unknown source {name!r}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"data-collector","msg":"%(message)s"}')

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=os.getenv("COLLECTOR_SOURCE", "fixture"),
                        choices=["fixture", "opendota", "opendota-public"])
    parser.add_argument("--interval", type=int,
                        default=int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "300")))
    parser.add_argument("--once", action="store_true",
                        help="один проход и выход (для тестов/CI)")
    args = parser.parse_args()

    source = build_source(args.source)
    cfg = CollectorConfig(
        postgres_dsn=os.getenv(
            "POSTGRES_DSN",
            "postgresql://dota:dota_dev_password@localhost:5432/dota_analyst"),
        kafka_brokers=os.getenv("KAFKA_BROKERS", "localhost:9092"),
        s3_endpoint=os.getenv("S3_ENDPOINT", "localhost:9500"),
        s3_access_key=os.getenv("S3_ACCESS_KEY", "dota"),
        s3_secret_key=os.getenv("S3_SECRET_KEY", "dota_dev_password"),
        s3_bucket=os.getenv("S3_BUCKET", "replays"),
    )

    metrics_port = int(os.getenv("METRICS_PORT", "9105"))
    if metrics_port and not args.once:
        start_http_server(metrics_port)
    collector = Collector(cfg, source)
    log = logging.getLogger("collector")
    try:
        while True:
            # Временный сбой внешнего API (5xx OpenDota, сеть) не должен
            # убивать демона — цикл повторится через interval.
            try:
                n = collector.collect_once()
                MATCHES_COLLECTED.inc(n)
                log.info("cycle done, processed=%s", n)
            except Exception:  # noqa: BLE001
                if args.once:
                    raise
                CYCLES_FAILED.inc()
                log.exception("цикл сбора упал; повтор через %ss", args.interval)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        collector.close()


if __name__ == "__main__":
    main()
