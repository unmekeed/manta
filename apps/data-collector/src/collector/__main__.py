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

from . import budget
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

# Пауза при исчерпанном бюджете. Час, а не «до полуночи»: потолок
# источника растёт в течение суток по мере таяния резерва под чужие
# гарантии, и проверять это раз в час дёшево — ни одного запроса к API
# такая проверка не стоит.
BUDGET_RETRY_S = int(os.getenv("BUDGET_RETRY_S", "3600"))

# Первая пауза после ОБРЫВА — не после отказа по квоте (спринт 146).
TRANSIENT_SLEEP_S = int(os.getenv("TRANSIENT_SLEEP_S", "300"))


def is_transient(exc: BaseException) -> bool:
    """Сбой на ТОЙ стороне или в сети — не наш отказ и не квота.

    Замер 2026-08-19: api.opendota.com отдавал 522 (Cloudflare не
    достучался до origin) — проверено с двух разных сетей, то есть лежала
    не машина. Такие обрывы длятся минуты.

    429 сюда НЕ попадает намеренно: это не обрыв, а исчерпанная квота, и
    у неё своя, куда более длинная пауза. Перепутать значило бы долбить
    API при отрицательном остатке.
    """
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500
    return False


class TransientBackoff:
    """Пауза после обрывов подряд: 300с, 600с, 1200с… но не дольше цикла.

    ЗАЧЕМ. У opendota-public интервал цикла — час. Двадцатисекундный
    обрыв у Cloudflare стоил ровно этого часа: цикл падал, и коллектор
    ложился спать на полный интервал, будто ничего не случилось. При том
    что через минуту всё работает.

    Почему не «повторить запрос прямо в цикле». Каждая попытка тратит
    бюджет вызовов (budget.spend() стоит ПЕРЕД запросом), а у
    opendota-public его 50 в сутки. Повтор внутри цикла жёг бы квоту тем
    быстрее, чем дольше лежит чужой сервер. Здесь цена обрыва — не лишние
    вызовы, а более ранний следующий цикл.

    Почему растущая, а не постоянная. При долгом падении внешнего API
    постоянные 300с дали бы двенадцать бесполезных циклов в час. Удвоение
    возвращает к обычному интервалу за четыре обрыва: короткий сбой стоит
    пяти минут, длинный — не дороже прежнего.
    """

    def __init__(self, base: int = TRANSIENT_SLEEP_S) -> None:
        self._base = base
        self.failures = 0

    def reset(self) -> None:
        """Цикл прошёл — счётчик обнуляется, иначе пауза росла бы вечно."""
        self.failures = 0

    def next_sleep(self, interval: int) -> int:
        self.failures += 1
        return min(interval, self._base * 2 ** (self.failures - 1))


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


def report_parked() -> None:
    """Что стоит в парковке и по чьей вине (спринт 153).

    Печатает, а не отдаёт метрику: вопрос задаётся руками и редко —
    «маршрут вернулся, есть ли к чему возвращаться». Заводить ради него
    экспортер значило бы копить в Prometheus число, на которое никто не
    смотрит.
    """
    import psycopg

    from .parked import ParkedStore

    dsn = os.getenv(
        "POSTGRES_DSN",
        "postgresql://dota:dota_dev_password@localhost:5432/manta")
    store = ParkedStore(psycopg.connect(dsn, autocommit=True))
    rows = store.stats()
    if not rows:
        print("парковка пуста")
        return
    total = sum(n for _, n in rows)
    print(f"ждут реплея: {total}")
    for host, n in rows:
        print(f"  {n:5d}  {host}")


# Имена источников — ОДИН список на весь модуль (спринт 180).
#
# До этого их было два: перечень в `choices` парсера и ветки самой
# фабрики. Они разъехались молча — `parked` и `salts` фабрика создавала, а
# парсер о них не знал. Держалось это на тонкости argparse: значение по
# умолчанию он по choices НЕ проверяет, поэтому через COLLECTOR_SOURCE
# проходило что угодно, а через `--source` то же имя отвергалось. Два
# способа задать одно и то же вели себя по-разному, и usage при этом
# показывал неполный список — то есть врал.
#
# Порядок здесь читаемый, а не алфавитный: сначала обычные источники,
# потом те, что запускаются по требованию.
SOURCES = (
    "fixture", "opendota", "opendota-public", "candidates",
    "opendota-timeline", "opendota-timeline-pro", "opendota-league",
    "stratz-timeline", "stratz-timeline-pro",
    # По требованию: возврат к непокорившимся реплеям и скачивание по
    # уже добытой соли (спринты 153 и 179).
    "parked", "salts",
)


def build_source(name: str):
    limit = int(os.getenv("OPENDOTA_LIMIT", "3"))
    api_key = os.getenv("OPENDOTA_API_KEY") or None
    shard = _shard_from_env()
    if name == "fixture":
        return FixtureSource()
    if name == "opendota":
        return OpenDotaSource(limit_per_cycle=limit, api_key=api_key,
                              shard=shard)
    if name == "parked":
        # Возврат к матчам, чей реплей однажды не взялся (спринт 153).
        # Запускается вручную: `COLLECTOR_SOURCE=parked ... --once`.
        from .parked import ParkedStore
        from .sources.parked import ParkedSource
        import psycopg
        dsn = os.getenv(
            "POSTGRES_DSN",
            "postgresql://dota:dota_dev_password@localhost:5432/manta")
        db = psycopg.connect(dsn, autocommit=True)
        return ParkedSource(
            ParkedStore(db),
            limit_per_cycle=int(os.getenv("PARKED_LIMIT", "5")),
            api_key=api_key, shard=shard)
    if name == "salts":
        # Матчи, соль которых уже добыта у GC (спринт 179). Ни OpenDota,
        # ни STEAM_API_KEY, ни квоты здесь не участвуют: всё нужное уже
        # лежит в базе.
        from .salts import SaltStore
        from .sources.salts import SaltSource
        import psycopg
        dsn = os.getenv(
            "POSTGRES_DSN",
            "postgresql://dota:dota_dev_password@localhost:5432/manta")
        db = psycopg.connect(dsn, autocommit=True)
        return SaltSource(
            SaltStore(db),
            limit_per_cycle=int(os.getenv("SALTS_LIMIT", "5")),
            api_key=api_key, shard=shard)
    if name == "candidates":
        # Своя разбивка: список матчей — от Valve, ранги — из кэша, у
        # OpenDota остаётся только соль. Один запрос на матч вместо
        # прежних двух-трёх.
        from .candidates import CandidateQueue
        from .sources.candidates import CandidateSource
        dsn = os.getenv(
            "POSTGRES_DSN",
            "postgresql://dota:dota_dev_password@localhost:5432/manta")
        from .ranks import RankCache
        return CandidateSource(
            CandidateQueue(dsn),
            limit_per_cycle=int(os.getenv("CANDIDATES_LIMIT", "20")),
            api_key=api_key, shard=shard, cache=RankCache(dsn))
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
            # Матчей на запрос. Дефолт 1: пакетный matches(ids:) отвечает
            # «User is not an admin» на Default-токене (спринт 122).
            batch_size=int(os.getenv("STRATZ_BATCH_SIZE", "1")),
        )
    # Имена перечисляются в отказе: COLLECTOR_SOURCE задаётся в env-файле
    # и в compose, где опечатку глазами не поймать, а «unknown source
    # 'slats'» без списка отправляет читать исходники.
    raise ValueError(
        f"неизвестный источник {name!r}. Доступны: {', '.join(SOURCES)}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"data-collector","msg":"%(message)s"}')

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=os.getenv("COLLECTOR_SOURCE", "fixture"),
                        choices=SOURCES)
    parser.add_argument("--interval", type=int,
                        default=int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "300")))
    parser.add_argument("--once", action="store_true",
                        help="один проход и выход (для тестов/CI)")
    parser.add_argument("--parked-report", action="store_true",
                        help="показать, что стоит в парковке, и выйти")
    args = parser.parse_args()

    if args.parked_report:
        report_parked()
        return

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

    # Бюджет вызовов OpenDota для ЭТОГО процесса (спринт 130). Без него
    # источник, тратящий больше своей доли, останавливает все остальные —
    # включая те, что кормят про-эталон промоушен-гейта.
    budget.budget_from_env(
        os.getenv("POSTGRES_DSN",
                  "postgresql://dota:dota_dev_password@localhost:5432/manta"),
        args.source)

    metrics_port = int(os.getenv("METRICS_PORT", default_metrics_port))
    if metrics_port and not args.once:
        manta_grpc.serve_metrics(metrics_port, "data-collector")
    log = logging.getLogger("collector")
    backoff = TransientBackoff()
    try:
        while True:
            # Временный сбой внешнего API (5xx OpenDota, сеть) не должен
            # убивать демона — цикл повторится РАНЬШЕ обычного интервала,
            # см. TransientBackoff. 429 — особый случай: обычный interval
            # означал бы ещё десяток бесполезных попыток до полуночи UTC,
            # каждая из которых всё равно немного дожигает и без того
            # отрицательный остаток квоты (см. docs/runbooks.md) — ждём
            # настоящего сброса.
            sleep_s = args.interval
            try:
                n = collector.collect_once()
                MATCHES_COLLECTED.inc(n)
                backoff.reset()
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
                elif is_transient(e):
                    sleep_s = backoff.next_sleep(args.interval)
                    log.warning(
                        "%s от %s — сбой на их стороне, не квота; повтор "
                        "через %ss вместо обычных %ss (обрывов подряд: %d)",
                        status, args.source, sleep_s, args.interval,
                        backoff.failures)
                else:
                    log.exception("цикл сбора упал; повтор через %ss",
                                  args.interval)
            except budget.BudgetExhausted as e:
                # Бюджет исчерпан. Это НЕ поломка: спим, как при 429, но
                # БЕЗ единого лишнего запроса — в том и смысл бюджета,
                # чтобы упереться в него раньше, чем в чужой лимит.
                #
                # Спать до полуночи БОЛЬШЕ НЕЛЬЗЯ. С работосохраняющим
                # бюджетом (спринт 135) потолок источника РАСТЁТ в
                # течение суток: резерв под невыбранные гарантии соседей
                # тает вместе с днём. Источник, упёршийся в потолок утром,
                # к вечеру почти наверняка сможет продолжить — а старая
                # логика укладывала его спать на восемнадцать часов и
                # оставляла квоту неиспользованной. Ровно это и было
                # замерено 2026-08-07: два источника спали при целой на
                # 85% квоте.
                if args.once:
                    raise
                RATE_LIMITED.inc()
                sleep_s = min(max(sleep_s, BUDGET_RETRY_S),
                              seconds_until_utc_midnight())
                log.warning("%s; повтор через ~%.1fч (потолок растёт по мере"
                            " суток)", e, sleep_s / 3600)
            except Exception as e:  # noqa: BLE001
                if args.once:
                    raise
                CYCLES_FAILED.inc()
                if is_transient(e):
                    # Обрыв связи не доходит до HTTPError вовсе: requests
                    # бросает ConnectionError/Timeout, и раньше такой сбой
                    # падал сюда, в общую ветку, стоя полного интервала.
                    sleep_s = backoff.next_sleep(args.interval)
                    log.warning(
                        "обрыв связи с %s (%s); повтор через %ss вместо "
                        "обычных %ss (обрывов подряд: %d)",
                        args.source, type(e).__name__, sleep_s,
                        args.interval, backoff.failures)
                else:
                    log.exception("цикл сбора упал; повтор через %ss",
                                  args.interval)
            if args.once:
                break
            time.sleep(sleep_s)
    finally:
        collector.close()


if __name__ == "__main__":
    main()
