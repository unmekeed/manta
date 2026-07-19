"""Точка входа Data Collector: периодический цикл сбора.

Запуск:  python -m collector [--source fixture|opendota] [--interval 300]
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from prometheus_client import Counter, start_http_server

from .runner import Collector, CollectorConfig

MATCHES_COLLECTED = Counter("matches_collected_total",
                            "Собранные и опубликованные матчи")
CYCLES_FAILED = Counter("collector_cycles_failed_total",
                        "Циклы сбора, упавшие по внешним причинам")
RATE_LIMITED = Counter("opendota_rate_limited_total",
                       "Циклы, оборванные 429 (квота OpenDota исчерпана)")
from .sources.fixture import FixtureSource
from .sources.opendota import OpenDotaSource
from .sources.opendota_public import OpenDotaPublicSource
from .sources.opendota_timeline import OpenDotaTimelineSource


def seconds_until_utc_midnight(now: datetime | None = None,
                               buffer_s: int = 120) -> int:
    """До заявленного сброса дневной квоты OpenDota (00:00 UTC) + запас.

    Запас нужен, чтобы не выстрелить циклом за секунду до реального
    сброса и не словить тот же 429 второй раз подряд.
    """
    now = now or datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds()) + buffer_s


def build_source(name: str):
    limit = int(os.getenv("OPENDOTA_LIMIT", "3"))
    api_key = os.getenv("OPENDOTA_API_KEY") or None
    if name == "fixture":
        return FixtureSource()
    if name == "opendota":
        return OpenDotaSource(limit_per_cycle=limit, api_key=api_key)
    if name == "opendota-public":
        min_patch = os.getenv("OPENDOTA_MIN_PATCH")
        return OpenDotaPublicSource(
            limit_per_cycle=limit,
            min_rank=int(os.getenv("OPENDOTA_MIN_RANK", "80")),
            min_patch=int(min_patch) if min_patch else None,
            api_key=api_key,
        )
    if name in ("opendota-timeline", "opendota-timeline-pro"):
        min_patch = os.getenv("OPENDOTA_MIN_PATCH")
        detail_budget = os.getenv("TIMELINE_DETAIL_BUDGET")
        return OpenDotaTimelineSource(
            limit_per_cycle=int(os.getenv("TIMELINE_LIMIT", "30")),
            min_rank=int(os.getenv("OPENDOTA_MIN_RANK", "80")),
            min_patch=int(min_patch) if min_patch else None,
            mode="pro" if name.endswith("-pro") else "public",
            api_key=api_key,
            detail_budget=int(detail_budget) if detail_budget else None,
        )
    raise ValueError(f"unknown source {name!r}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"data-collector","msg":"%(message)s"}')

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=os.getenv("COLLECTOR_SOURCE", "fixture"),
                        choices=["fixture", "opendota", "opendota-public",
                                 "opendota-timeline", "opendota-timeline-pro"])
    parser.add_argument("--interval", type=int,
                        default=int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "300")))
    parser.add_argument("--once", action="store_true",
                        help="один проход и выход (для тестов/CI)")
    args = parser.parse_args()

    source = build_source(args.source)
    if args.source.startswith("opendota-timeline"):
        # JSON-путь: без S3/Kafka — витрина пишется напрямую.
        from .timeline_runner import TimelineCollector, TimelineConfig
        collector = TimelineCollector(TimelineConfig(), source)
        default_metrics_port = ("9108" if args.source == "opendota-timeline"
                                else "9110")
    else:
        cfg = CollectorConfig(
            postgres_dsn=os.getenv(
                "POSTGRES_DSN",
                "postgresql://dota:dota_dev_password@localhost:5432/manta"),
            kafka_brokers=os.getenv("KAFKA_BROKERS", "localhost:9092"),
            s3_endpoint=os.getenv("S3_ENDPOINT", "localhost:9500"),
            s3_access_key=os.getenv("S3_ACCESS_KEY", "dota"),
            s3_secret_key=os.getenv("S3_SECRET_KEY", "dota_dev_password"),
            s3_bucket=os.getenv("S3_BUCKET", "replays"),
        )
        collector = Collector(cfg, source)
        default_metrics_port = "9105"

    metrics_port = int(os.getenv("METRICS_PORT", default_metrics_port))
    if metrics_port and not args.once:
        start_http_server(metrics_port)
    log = logging.getLogger("collector")
    try:
        while True:
            # Временный сбой внешнего API (5xx OpenDota, сеть) не должен
            # убивать демона — цикл повторится через interval. 429 —
            # особый случай: обычный interval означал бы ещё десяток
            # бесполезных попыток до полуночи UTC, каждая из которых всё
            # равно немного дожигает и без того отрицательный остаток
            # квоты (см. docs/runbooks.md) — ждём настоящего сброса.
            sleep_s = args.interval
            try:
                n = collector.collect_once()
                MATCHES_COLLECTED.inc(n)
                log.info("cycle done, processed=%s", n)
            except requests.HTTPError as e:
                if args.once:
                    raise
                CYCLES_FAILED.inc()
                if e.response is not None and e.response.status_code == 429:
                    RATE_LIMITED.inc()
                    remaining = e.response.headers.get(
                        "x-rate-limit-remaining-day", "?")
                    sleep_s = max(sleep_s, seconds_until_utc_midnight())
                    log.warning(
                        "429: квота OpenDota исчерпана (remaining-day=%s); "
                        "жду сброса ~%.1fч вместо обычных %ss — см. "
                        "docs/runbooks.md и OPENDOTA_API_KEY",
                        remaining, sleep_s / 3600, args.interval)
                else:
                    log.exception("цикл сбора упал; повтор через %ss",
                                  args.interval)
            except Exception:  # noqa: BLE001
                if args.once:
                    raise
                CYCLES_FAILED.inc()
                log.exception("цикл сбора упал; повтор через %ss", args.interval)
            if args.once:
                break
            time.sleep(sleep_s)
    finally:
        collector.close()


if __name__ == "__main__":
    main()
