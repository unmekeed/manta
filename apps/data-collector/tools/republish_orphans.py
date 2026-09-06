"""Переотправка матчей, чей реплей есть, а разбора нет (спринт 194).

    PYTHONPATH=src:../../libs python3 tools/republish_orphans.py --dry-run
    PYTHONPATH=src:../../libs python3 tools/republish_orphans.py

ЗАЧЕМ. С 3 по 6 сентября 2026 топики Kafka отсутствовали, а коллектор
публикацию не проверял: матч скачивался, ложился в S3, ПОМЕЧАЛСЯ
СОБРАННЫМ — и его событие пропадало. Помеченный матч больше не берётся
никогда, поэтому трое суток реплеев выпали из обработки насовсем.
Проверка публикации (спринт 194) закрывает будущее; этот инструмент
разбирает прошлое.

КАК НАХОДЯТСЯ СИРОТЫ. Пересечением двух источников: в Postgres матч
помечен `has_replay = TRUE`, а в ClickHouse у него нет ни одной строки в
`ReplayEvents`. Отдельного состояния «опубликован» никто не хранит, и
заводить его ради разовой починки не нужно: отсутствие результата —
более честный признак, чем любой флаг о намерении.

ЧЕГО ИНСТРУМЕНТ НЕ ДЕЛАЕТ. Не качает реплеи заново. Если объекта в S3
уже нет (разобранный реплей удаляется, а неразобранный мог попасть под
ретеншен), матч ПРОПУСКАЕТСЯ и попадает в отчёт отдельной строкой.
Публиковать событие со ссылкой на несуществующий объект значило бы
насыпать парсеру работы, которая заведомо кончится ошибкой, — и снова
сделать настоящую потерю неотличимой от шума.

БЕЗОПАСНОСТЬ ПОВТОРА. Разбор идемпотентен: ReplayEvents — ReplacingMerge‐
Tree, повторная вставка тех же строк схлопнется. Так что худшее, что
может случиться при лишней публикации, — потраченное время парсера.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
import requests
from confluent_kafka import Producer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from collector.runner import TOPIC, build_envelope  # noqa: E402
from collector.sources import MatchRef  # noqa: E402

# Кандидаты: реплей числится за нами. Сироты отбираются дальше, по
# отсутствию строк в витрине, — одним запросом это не сделать, базы разные.
CANDIDATES_SQL = """
SELECT match_id, source_name, replay_url
  FROM CollectedMatches
 WHERE has_replay = TRUE AND match_id > %(since)s
 ORDER BY match_id DESC
 LIMIT %(limit)s
"""

PARSED_SQL = """
SELECT DISTINCT match_id FROM manta.ReplayEvents
 WHERE match_id IN ({ids})
"""

# Уровень матча (tier) не хранится в CollectedMatches, а подставлять его
# наугад нельзя: tier задаёт вес матча при обучении, и ошибка была бы
# тихой — модель молча училась бы на неверно взвешенных данных.
# Единственный честный источник — витрина, куда его записал тот путь,
# который матч и собрал.
TIER_SQL = """
SELECT match_id, any(tier) AS tier, any(patch) AS patch
  FROM manta.MatchTimelineFeatures
 WHERE match_id IN ({ids})
 GROUP BY match_id
"""


def ch_rows(query: str, match_ids: list[int]) -> list[dict]:
    if not match_ids:
        return []
    ids = ",".join(str(int(m)) for m in match_ids)
    resp = requests.post(
        os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123"),
        params={"database": os.getenv("CLICKHOUSE_DB", "manta"),
                "default_format": "JSONEachRow"},
        data=query.format(ids=ids),
        headers={"X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
                 "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                               "dota_dev_password")},
        timeout=120)
    resp.raise_for_status()
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def ch_parsed(match_ids: list[int]) -> set[int]:
    """Какие из матчей уже разобраны."""
    return {int(r["match_id"]) for r in ch_rows(PARSED_SQL, match_ids)}


def ch_tiers(match_ids: list[int]) -> dict[int, tuple[str, int]]:
    """match_id → (tier, patch) из витрины; чего нет — того нет."""
    return {int(r["match_id"]): (str(r.get("tier") or ""),
                                 int(r.get("patch") or 0))
            for r in ch_rows(TIER_SQL, match_ids)}


def s3_client():
    from minio import Minio

    return Minio(os.getenv("S3_ENDPOINT", "127.0.0.1:9500"),
                 access_key=os.getenv("S3_ACCESS_KEY", "dota"),
                 secret_key=os.getenv("S3_SECRET_KEY", "dota_dev_password"),
                 secure=os.getenv("S3_USE_SSL", "0") == "1")


def parse_url(replay_url: str) -> tuple[str, str] | None:
    """`s3://bucket/key` → (bucket, key); иначе None."""
    if not replay_url.startswith("s3://"):
        return None
    rest = replay_url[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return (bucket, key) if bucket and key else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0,
                    help="искать только матчи новее этого match_id")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dsn = os.getenv("POSTGRES_DSN",
                    "postgresql://dota:dota_dev_password@127.0.0.1:5432/manta")
    with psycopg.connect(dsn, autocommit=True) as db:
        rows = db.execute(CANDIDATES_SQL, {"since": args.since,
                                           "limit": args.limit}).fetchall()
    print(f"матчей с реплеем: {len(rows)}")

    parsed = ch_parsed([r[0] for r in rows])
    orphans = [r for r in rows if r[0] not in parsed]
    print(f"уже разобрано: {len(parsed)}")
    print(f"сирот (реплей есть, разбора нет): {len(orphans)}")
    if not orphans:
        return 0

    tiers = ch_tiers([r[0] for r in orphans])
    s3 = s3_client()
    failures: list[str] = []

    def on_delivery(err, _msg):
        if err is not None:
            failures.append(str(err))

    producer = Producer({"bootstrap.servers":
                         os.getenv("KAFKA_BROKERS", "127.0.0.1:9092")})
    sent = gone = unknown_tier = 0
    for match_id, source_name, replay_url in orphans:
        if match_id not in tiers:
            # Витрина про этот матч ничего не знает, значит уровень
            # взять неоткуда. Публиковать с выдуманным tier значит
            # подмешать в обучение неверно взвешенный матч — тихо и
            # необратимо. Лучше не тронуть и сказать об этом.
            unknown_tier += 1
            continue
        loc = parse_url(replay_url)
        if loc is None:
            gone += 1
            continue
        bucket, key = loc
        try:
            s3.stat_object(bucket, key)
        except Exception:  # noqa: BLE001 — объекта нет или хранилище молчит
            gone += 1
            continue
        if args.dry_run:
            if sent < 5:
                print(f"  переотправил бы {match_id} ({replay_url})")
            sent += 1
            continue
        tier, patch = tiers[match_id]
        env = build_envelope(
            MatchRef(match_id=match_id, replay_url=replay_url, tier=tier,
                     source_cursor=str(match_id), patch=patch),
            replay_url, source_name or "republish")
        producer.produce(TOPIC, key=env["partition_key"].encode(),
                         value=json.dumps(env).encode(),
                         on_delivery=on_delivery)
        sent += 1

    if not args.dry_run:
        remaining = producer.flush(30)
        if remaining or failures:
            print(f"ВНИМАНИЕ: не доставлено {remaining}, "
                  f"ошибки: {'; '.join(failures[:3])}")
            return 1
    print(("переотправил бы" if args.dry_run else "переотправлено")
          + f": {sent}")
    print(f"пропущено (реплея в S3 уже нет): {gone}")
    print(f"пропущено (уровень матча неизвестен): {unknown_tier}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
