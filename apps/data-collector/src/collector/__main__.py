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
import manta_grpc
from prometheus_client import Counter

from .runner import Collector, CollectorConfig

MATCHES_COLLECTED = Counter("matches_collected_total",
                            "Собранные и опубликованные матчи")
CYCLES_FAILED = Counter("collector_cycles_failed_total",
                        "Циклы сбора, упавшие по внешним причинам")
RATE_LIMITED = Counter("opendota_rate_limited_total",
                       "Циклы, оборванные 429 (квота OpenDota исчерпана)")

# Запас суточной квоты, ниже которого 429 считается исчерпанием суток, а
# не минутным всплеском. Не ноль: заголовок отдаёт остаток на момент
# ответа, и у самой границы оба лимита срабатывают вперемешку.
BURST_DAY_MARGIN = 50
from .sources import Shard, SourceSplit
from .sources.fixture import FixtureSource
from .sources.opendota import OpenDotaSource
from .sources.opendota_public import OpenDotaPublicSource
from .sources.opendota_timeline import OpenDotaTimelineSource
from .sources.stratz import StratzTimelineSource


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


def _shard_from_env() -> Shard:
    """Шард сбора из окружения (несколько машин с разными IP делят поток
    матчей без пересечения). SHARD_COUNT=1 (дефолт) — одиночная машина.
    Одинаковый шард на обеих машинах кластера, разный SHARD_ID."""
    return Shard(shard_id=int(os.getenv("COLLECTOR_SHARD_ID", "0")),
                 count=int(os.getenv("COLLECTOR_SHARD_COUNT", "1")))


def _detail_split(name: str) -> SourceSplit:
    """Доля кандидатов этого источника среди источников деталей машины.

    JSON-источники OpenDota и STRATZ читают ОДИН И ТОТ ЖЕ листинг с
    вершины. Без разделения они каждый цикл берут одни и те же свежие
    матчи: CollectedMatches отсекает повтор лишь ПОСЛЕ отметки, а между
    проверкой и отметкой лежит запрос деталей. Проигрыш в этой гонке
    стоит не лишнего запроса, а фич — витрина ReplacingMergeTree
    оставляет строку, вставленную последней, и строка STRATZ затирает
    строку OpenDota вместе с треком F.

    Пока STRATZ не настроен, делить не с кем — фильтр пропускает всё.

    ПРО-МАТЧИ НЕ ДЕЛЯТСЯ (спринт 93). Деление имеет смысл, когда
    кандидатов больше, чем мы успеваем обработать; у /proMatches всё
    ровно наоборот. Окно источника — около тысячи матчей, шард машины
    режет его пополам, и оставшееся почти целиком состоит из уже
    собранного: цикл stratz-pro стабильно давал «собрано 0 из 1000
    (дубликат: 238)». Половина дефицитного ресурса при этом уходила
    источнику, который не умеет ни трек F, ни networth_total.
    А про-матчи — это ЭТАЛОН гейта: пока он не растёт, каждое
    переобучение сравнивается со всё более старой выборкой.
    Поэтому весь про-поток отдаётся OpenDota, а stratz-timeline-pro не
    запускается вовсе (см. dev-recover.sh).
    """
    if (name.endswith("-pro") or name == "opendota-league"
            or not os.getenv("STRATZ_API_TOKEN")):
        return SourceSplit()
    return SourceSplit(split_id=1 if name.startswith("stratz") else 0,
                       count=2)


def blamed_on_stratz(response, source: str) -> bool:
    """Кто на самом деле ответил ошибкой — STRATZ или OpenDota.

    Раньше решалось по ИМЕНИ источника: любой 429 в stratz-коллекторе
    считался лимитом STRATZ и усыплял его на час. Но кандидатов stratz
    берёт из ЛИСТИНГА OPENDOTA, у которого свой минутный лимит 60/мин —
    и он куда жёстче, потому что его делят все коллекторы машины.

    Замер 2026-08-06: три 429 за сутки при остатке квоты STRATZ 13358 из
    15000 (то есть у STRATZ упереться было не во что) и remaining-minute
    у OpenDota 23 из 60. Каждый такой 429 стоил часа простоя источника,
    который к тому моменту стал главным по притоку — 197 матчей в сутки.

    Отсюда правило: смотрим на URL ОТВЕТА. Имя источника остаётся
    запасным вариантом на случай, когда ответа нет вовсе (обрыв связи).
    """
    url = str(getattr(response, "url", "") or "")
    if url:
        return "stratz" in url.lower()
    return source.startswith("stratz")


def build_source(name: str):
    limit = int(os.getenv("OPENDOTA_LIMIT", "3"))
    api_key = os.getenv("OPENDOTA_API_KEY") or None
    shard = _shard_from_env()
    if name == "fixture":
        return FixtureSource()
    if name == "opendota":
        return OpenDotaSource(limit_per_cycle=limit, api_key=api_key,
                              shard=shard)
    if name == "opendota-public":
        min_patch = os.getenv("OPENDOTA_MIN_PATCH")
        return OpenDotaPublicSource(
            limit_per_cycle=limit,
            min_rank=int(os.getenv("OPENDOTA_MIN_RANK", "80")),
            min_patch=int(min_patch) if min_patch else None,
            api_key=api_key,
            shard=shard,
        )
    if name in ("opendota-timeline", "opendota-timeline-pro",
                "opendota-league"):
        min_patch = os.getenv("OPENDOTA_MIN_PATCH")
        detail_budget = os.getenv("TIMELINE_DETAIL_BUDGET")
        return OpenDotaTimelineSource(
            limit_per_cycle=int(os.getenv("TIMELINE_LIMIT", "30")),
            min_rank=int(os.getenv("OPENDOTA_MIN_RANK", "80")),
            min_patch=int(min_patch) if min_patch else None,
            mode=("league" if name == "opendota-league"
                  else "pro" if name.endswith("-pro") else "public"),
            api_key=api_key,
            detail_budget=int(detail_budget) if detail_budget else None,
            shard=shard,
            split=_detail_split(name),
            league_tiers=os.getenv("LEAGUE_TIERS", "premium,professional"),
            league_batch=int(os.getenv("LEAGUE_BATCH", "8")),
            league_max_age_days=int(os.getenv("LEAGUE_MAX_AGE_DAYS", "30")),
        )
    if name in ("stratz-timeline", "stratz-timeline-pro"):
        # Кандидаты — дешёвый листинг OpenDota, детали — GraphQL STRATZ.
        # Суточный потолок здесь свой (10000 у Default-токена), поэтому
        # лимит цикла задаётся отдельной переменной, а не OPENDOTA_LIMIT.
        min_patch = os.getenv("STRATZ_MIN_PATCH")
        stratz_budget = os.getenv("STRATZ_DETAIL_BUDGET")
        return StratzTimelineSource(
            token=os.getenv("STRATZ_API_TOKEN", ""),
            limit_per_cycle=int(os.getenv("STRATZ_LIMIT", "40")),
            min_patch=int(min_patch) if min_patch else None,
            # Тот же порог, что у JSON-источника OpenDota: без него
            # ярлык tier='Premium' означал у двух источников разные
            # популяции (спринт 94).
            min_rank=int(os.getenv("STRATZ_MIN_RANK",
                                   os.getenv("OPENDOTA_MIN_RANK", "80"))),
            mode="pro" if name.endswith("-pro") else "public",
            opendota_key=api_key,
            kills_cumulative=os.getenv("STRATZ_KILLS_CUMULATIVE") == "1",
            shard=shard,
            split=_detail_split(name),
            # Свежий матч, которого STRATZ ещё не распарсил, пробуется
            # несколько циклов вместо вечного отказа (спринт 87).
            retry_attempts=int(os.getenv("STRATZ_RETRY_ATTEMPTS", "3")),
            detail_budget=int(stratz_budget) if stratz_budget else None,
            # Отступ от вершины листинга: STRATZ отстаёт от OpenDota, и
            # без него 87 вызовов из 100 уходили на матчи, которых у
            # него ещё нет (спринт 95). Для pro не нужен: лиговые матчи
            # STRATZ разбирает сразу, а кандидатов там и так дефицит.
            # Отступ «в записях» оставлен нулевым: он зависел от того,
            # сколько матчей успел распарсить OpenDota, а не от того,
            # сколько времени было у STRATZ (спринт 114).
            skip_freshest=int(os.getenv("STRATZ_SKIP_FRESHEST", "0")),
            # Отступ в единицах match_id — это отступ во времени: id
            # растут ~789 в минуту. Стартовое значение подхватывается из
            # окружения, дальше источник ведёт его сам по доле промахов;
            # в лог пишется текущее, чтобы после рестарта можно было
            # стартовать с уже найденного. Для pro не нужен: лиговые
            # матчи STRATZ разбирает сразу, а кандидатов там дефицит.
            id_lag=0 if name.endswith("-pro") else
                   int(os.getenv("STRATZ_ID_LAG", "90000")),
            id_lag_min=int(os.getenv("STRATZ_ID_LAG_MIN", "30000")),
            id_lag_max=int(os.getenv("STRATZ_ID_LAG_MAX", "400000")),
            # Запас суточной квоты, на котором цикл останавливается сам.
            # Дешевле недобрать полтысячи вызовов, чем получить 429 и
            # потерять час простоя (спринт 119).
            quota_floor=int(os.getenv("STRATZ_QUOTA_FLOOR", "500")),
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
                                 "opendota-timeline", "opendota-timeline-pro",
                                 "opendota-league",
                                 "stratz-timeline", "stratz-timeline-pro"])
    parser.add_argument("--interval", type=int,
                        default=int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "300")))
    parser.add_argument("--once", action="store_true",
                        help="один проход и выход (для тестов/CI)")
    args = parser.parse_args()

    source = build_source(args.source)
    if args.source.startswith(("opendota-timeline", "opendota-league",
                               "stratz-timeline")):
        # JSON-путь: без S3/Kafka — витрина пишется напрямую.
        from .timeline_runner import TimelineCollector, TimelineConfig
        collector = TimelineCollector(TimelineConfig(), source)
        default_metrics_port = {
            "opendota-timeline": "9108",
            "opendota-timeline-pro": "9110",
            "opendota-league": "9117",
            "stratz-timeline": "9115",
            "stratz-timeline-pro": "9116",
        }[args.source]
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
        manta_grpc.serve_metrics(metrics_port, "data-collector")
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
                status = (e.response.status_code
                          if e.response is not None else None)
                from_stratz = blamed_on_stratz(e.response, args.source)
                if status == 429 and from_stratz:
                    RATE_LIMITED.inc()
                    # У STRATZ лимиты почасовые (2000/ч) и суточные
                    # (10000/сут), полночь UTC к ним отношения не имеет —
                    # ждать до неё значило бы простаивать зря.
                    sleep_s = max(sleep_s, int(os.getenv(
                        "STRATZ_RATE_SLEEP_S", "3600")))
                    log.warning(
                        "429: лимит STRATZ исчерпан; жду %ss вместо обычных "
                        "%ss — при регулярных 429 снижать STRATZ_LIMIT",
                        sleep_s, args.interval)
                elif status in (401, 403) and from_stratz:
                    # Токен протух или отозван: повтор через interval ничего
                    # не изменит, но и падать демону незачем — так проблема
                    # видна в логе и на дашборде, а не превращается в
                    # молчаливо мёртвый процесс.
                    log.error(
                        "%s от STRATZ: токен недействителен — проверить "
                        "STRATZ_API_TOKEN в ~/manta-train.env", status)
                elif status == 429:
                    RATE_LIMITED.inc()
                    remaining = e.response.headers.get(
                        "x-rate-limit-remaining-day", "?")
                    # У OpenDota ДВА лимита: суточный (~2000) и burst
                    # 60/мин. Раньше любой 429 усыплял коллектор до
                    # полуночи UTC — и всплеск на несколько секунд стоил
                    # 14 часов простоя (инцидент 2026-08-01: сон при
                    # remaining-day=2030, то есть при целой квоте).
                    # Различаем по остатку суточной: он цел → это burst.
                    day_left = None
                    if remaining not in ("", "?", None):
                        try:
                            day_left = int(remaining)
                        except ValueError:
                            day_left = None
                    if day_left is not None and day_left > BURST_DAY_MARGIN:
                        # ПРИСВАИВАЕМ, а не max(): sleep_s здесь уже равен
                        # интервалу коллектора (1800с), и max оставлял бы
                        # его — то есть за минутный всплеск платили бы
                        # получасом. Цикл при этом оборван на середине,
                        # часть кандидатов не обработана, и повторить его
                        # нужно СКОРО, как только сбросится burst.
                        sleep_s = int(os.getenv("OPENDOTA_BURST_SLEEP_S", "90"))
                        log.warning(
                            "429 при целой суточной квоте (remaining-day=%s) "
                            "— это минутный лимит 60/мин; жду %ss. Если "
                            "повторяется, разнести коллекторы по времени "
                            "или снизить лимиты",
                            remaining, sleep_s)
                    else:
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
