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
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from confluent_kafka import Producer
from minio import Minio

from .parked import (ParkedStore, UnreachableHosts,  # noqa: F401
                     is_unreachable, replay_host)
from .sources import (IncompleteDownloadError, MatchRef,
                      PermanentDownloadError, Source)

logger = logging.getLogger("collector")

PRODUCER_NAME = "data-collector@0.1.0"
TOPIC = "match.downloaded"

# Сколько раз подряд повторять матч, упавший с ВРЕМЕННОЙ ошибкой, прежде
# чем сдвинуть курсор через него. Очередь важнее одного матча: при
# часовом интервале это максимум 3 часа простоя вместо бесконечного.
MAX_TRANSIENT_RETRIES = 3

# Сколько суток держать .dem в S3 после выгрузки.
#
# До этого не удалялось НИЧЕГО: ни политики, ни вызова remove_object во
# всём монорепо — реплеи копились вечно. Дома это терпимо, потому что
# матчей мало; при цели 2000 матчей в сутки это 113 ГиБ в день и 3.4 ТБ
# в месяц, то есть переполнение диска VPS на первой же неделе.
#
# Трое суток. Срок выбран по деньгам и по тому, ради чего реплеи вообще
# хранятся, — а хранятся они ради ПЕРЕРАЗБОРА, когда парсер научится
# извлекать новое (так было в треках F и G).
#
# Арифметика, которую стоит держать перед глазами (58 МиБ на матч):
#
#     темп      3 суток      14 суток
#      300        51 ГиБ       238 ГиБ
#      600       102 ГиБ       476 ГиБ
#     2000       340 ГиБ      1586 ГиБ
#
# При нынешних 300–600 матчах в сутки трое суток стоят около сотни
# рублей в месяц дополнительного диска — незаметно. При целевых 2000 и
# сроке в две недели это уже 2379 ₽/мес, дороже самого сервера. Порог
# полезно знать, упираться в него сегодня незачем.
#
# Важно: для переразбора хранить сам реплей не обязательно. Пока Valve
# держит его у себя (около двух недель), достаточно СОЛИ — двадцать
# байт против 58 мегабайт. Соль всё равно нужна GC-пути, и когда она
# будет храниться, этот срок можно сокращать смелее.
#
# Само удаление делает MinIO по расписанию: наш код только объявляет
# правило. Это надёжнее собственного сборщика мусора — он умирает вместе
# с процессом, а правило переживает перезапуски.
REPLAY_RETENTION_DAYS = int(os.getenv("REPLAY_RETENTION_DAYS", "3"))


def _ensure_replay_retention(client: Minio, bucket: str) -> None:
    """Объявить правило удаления .dem, если его ещё нет.

    Идемпотентно и не роняет коллектор: MinIO старых версий и совместимые
    S3 без поддержки lifecycle просто откажут, и это не повод не собирать
    матчи. Но молчать нельзя — без правила диск кончится, и знать об этом
    надо заранее, а не по «no space left on device».
    """
    from minio.commonconfig import ENABLED, Filter
    from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

    if REPLAY_RETENTION_DAYS <= 0:
        logger.warning("REPLAY_RETENTION_DAYS=%d — реплеи НЕ удаляются, "
                       "диск будет расти на ~58 МиБ с каждого матча",
                       REPLAY_RETENTION_DAYS)
        return
    try:
        current = client.get_bucket_lifecycle(bucket)
    except Exception:  # noqa: BLE001 — правила нет либо не поддерживается
        current = None
    if current and current.rules:
        return
    config = LifecycleConfig([
        Rule(status=ENABLED, rule_id="manta-replay-retention",
             rule_filter=Filter(prefix=""),
             expiration=Expiration(days=REPLAY_RETENTION_DAYS)),
    ])
    try:
        client.set_bucket_lifecycle(bucket, config)
        logger.info("правило хранения реплеев: удаление через %d суток",
                    REPLAY_RETENTION_DAYS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("не удалось задать правило хранения реплеев (%s). "
                       "Реплеи будут копиться — следи за диском", exc)


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
            "patch": ref.patch,
            "source": source_name,
        },
    }


class Collector:
    def __init__(self, cfg: CollectorConfig, source: Source) -> None:
        self._cfg = cfg
        self._source = source
        self._db = psycopg.connect(cfg.postgres_dsn, autocommit=False)
        self._producer = Producer({"bootstrap.servers": cfg.kafka_brokers})
        # match_id -> сколько циклов подряд он падал с временной ошибкой.
        # В памяти намеренно: счётчик нужен лишь чтобы разомкнуть затор,
        # и рестарт процесса — сам по себе достаточный повод дать матчу
        # ещё один шанс.
        self._transient_fails: dict[int, int] = {}
        # Хосты, к которым сейчас бессмысленно обращаться, и список
        # желаний для матчей с них (спринт 153). Отметка о хосте — в
        # памяти: маршрут меняется, и рестарт процесса сам по себе
        # достаточный повод попробовать снова. Сам матч — в базе, иначе
        # рестарт стёр бы его насовсем, а именно потерю мы и чиним.
        self._unreachable = UnreachableHosts(
            threshold=int(os.getenv("UNREACHABLE_AFTER", "2")),
            ttl_s=float(os.getenv("UNREACHABLE_TTL_S", str(6 * 3600))))
        self._parked = ParkedStore(lambda: self._db)
        self._s3 = Minio(cfg.s3_endpoint, access_key=cfg.s3_access_key,
                         secret_key=cfg.s3_secret_key, secure=cfg.s3_secure)
        if not self._s3.bucket_exists(cfg.s3_bucket):
            self._s3.make_bucket(cfg.s3_bucket)
        _ensure_replay_retention(self._s3, cfg.s3_bucket)

    # -- persistence ---------------------------------------------------------

    def _ensure_db(self) -> None:
        """Пересоздать мёртвое PG-соединение (рестарт контейнера/Docker
        Desktop): без этого коллектор вечно падал бы на первом же запросе
        цикла до ручного перезапуска процесса (инцидент 2026-07-20)."""
        if not self._db.closed:
            try:
                with self._db.cursor() as cur:
                    cur.execute("SELECT 1")
                self._db.rollback()   # не держим пустую транзакцию ping'а
                return
            except psycopg.OperationalError:
                try:
                    self._db.close()
                except Exception:  # noqa: BLE001
                    pass
        logger.warning("postgres: соединение умерло — переподключаюсь")
        self._db = psycopg.connect(self._cfg.postgres_dsn, autocommit=False)

    def _get_cursor(self) -> str | None:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT cursor_value FROM CollectorCursor WHERE source_name = %s",
                (self._source.name,))
            row = cur.fetchone()
            return row[0] if row else None

    def _is_collected(self, match_id: int) -> bool:
        """Есть ли у матча РЕПЛЕЙ — не «собран ли он вообще» (спринт 151).

        Разница решающая. CollectedMatches одна на все источники, и матч,
        взятый JSON-путём, лежит в ней наравне с разобранным из реплея. Но
        у JSON-матча нет ни позиций, ни событий: ни одной тепловой карты по
        нему не построить. Считая его дубликатом, реплейный путь отказывался
        от единственного источника координат — навсегда и молча.

        На VPS это дало 106 матчей в витрине при пустом PositionSnapshots:
        JSON-источники быстрее и жаднее (14 матчей за 30 минут против 2 за
        час) и разбирали те же про-матчи первыми.

        Обратное направление остаётся строгим: JSON-путь пропускает всё,
        что собрано любым путём, — реплей даёт всё то же и сверх того.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM CollectedMatches"
                " WHERE match_id = %s AND has_replay", (match_id,))
            return cur.fetchone() is not None

    def _mark_collected(self, ref: MatchRef, replay_url: str) -> None:
        with self._db.cursor() as cur:
            # DO UPDATE, а не DO NOTHING: матч мог прийти раньше
            # JSON-путём, и строка уже есть. Оставь мы DO NOTHING —
            # has_replay навсегда остался бы FALSE, реплейный путь считал
            # бы матч несобранным и качал его каждый цикл. Дедуп,
            # починенный наполовину, хуже сломанного: он жжёт квоту.
            cur.execute(
                """INSERT INTO CollectedMatches
                       (match_id, source_name, replay_url, has_replay)
                   VALUES (%s, %s, %s, TRUE)
                   ON CONFLICT (match_id) DO UPDATE
                       SET source_name = EXCLUDED.source_name,
                           replay_url  = EXCLUDED.replay_url,
                           has_replay  = TRUE""",
                (ref.match_id, self._source.name, replay_url))
            self._write_cursor(cur, ref)
        self._db.commit()

    def _write_cursor(self, cur, ref: MatchRef) -> None:
        cur.execute(
            """INSERT INTO CollectorCursor (source_name, cursor_value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (source_name)
               DO UPDATE SET cursor_value = EXCLUDED.cursor_value,
                             updated_at = NOW()""",
            (self._source.name, ref.source_cursor))

    def _advance_cursor(self, ref: MatchRef) -> None:
        """Сдвинуть курсор ЧЕРЕЗ матч, который собрать не удалось.

        Без этого шага источник блокируется навсегда: fetch_new отдаёт
        кандидатов старыми вперёд, отбрасывая всё <= курсора, поэтому
        несобираемый матч возвращается первым в каждом следующем цикле
        (инцидент 2026-07-31). Матч в CollectedMatches НЕ пишется — он
        не собран, и запись означала бы обратное.
        """
        with self._db.cursor() as cur:
            self._write_cursor(cur, ref)
        self._db.commit()

    def _park(self, ref: MatchRef, reason: str) -> None:
        """Записать матч в парковку. Сбой парковки не роняет цикл.

        Парковка — улучшение, а не обязательство: до спринта 153 матч
        просто терялся, и вернуться к прежнему поведению из-за недоступной
        таблицы честнее, чем остановить сбор целиком.
        """
        try:
            self._parked.park(ref.match_id, ref.replay_url, reason)
            self._db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("match %s: не удалось припарковать", ref.match_id,
                           exc_info=True)
            try:
                self._db.rollback()
            except Exception:  # noqa: BLE001
                pass

    # -- pipeline ------------------------------------------------------------

    def collect_once(self) -> int:
        """Один проход сбора; возвращает число обработанных матчей."""
        self._ensure_db()
        cursor = self._get_cursor()
        processed = 0
        for ref in self._source.fetch_new(cursor):
            # Дубликат — курсор ОБЯЗАН сдвинуться, иначе источник упрётся
            # в него каждый цикл навсегда (инцидент 2026-07-31).
            #
            # Дубликатом здесь считается матч, у которого УЖЕ ЕСТЬ реплей.
            # Матч, взятый JSON-путём, дубликатом не считается: у него нет
            # ни позиций, ни событий, то есть ни одной тепловой карты по
            # нему не построить (спринт 151, см. _is_collected).
            if self._is_collected(ref.match_id):
                logger.info("skip duplicate match_id=%s", ref.match_id)
                self._advance_cursor(ref)
                continue

            # Хост уже признан недостижимым — не набираем его номер
            # снова. Матч при этом НЕ теряется: он уходит в парковку, и
            # `--source parked` вернётся к нему, когда маршрут появится
            # (спринт 153).
            host = replay_host(ref.replay_url)
            if self._unreachable.is_unreachable(host):
                self._park(ref, f"хост {host} недостижим")
                logger.info("match %s: %s недостижим — паркую без попытки",
                            ref.match_id, host)
                self._advance_cursor(ref)
                continue

            # Сбой одного матча (503 реплей-сервера, битый bz2, сеть) не
            # должен ронять весь цикл (Гл. 2.4.2): логируем и идём дальше.
            try:
                data = self._source.download_replay(ref)
                self._unreachable.record_success(host)
            except PermanentDownloadError as exc:
                logger.warning("match %s: %s — пропускаю навсегда",
                               ref.match_id, exc)
                self._advance_cursor(ref)
                continue
            except IncompleteDownloadError as exc:
                # Намеренно НЕ считается постоянной: файл цел, оборвалась
                # передача. Падает в общую ветку временных сбоев ниже —
                # повторим, и только после MAX_TRANSIENT_RETRIES уйдём
                # дальше, чтобы не встать на одном матче.
                logger.warning("match %s: %s", ref.match_id, exc)
                n = self._transient_fails.get(ref.match_id, 0) + 1
                self._transient_fails[ref.match_id] = n
                if n >= MAX_TRANSIENT_RETRIES:
                    # Паркуем и здесь. Оборванная передача исчерпала
                    # попытки — матч уходит вперёд по курсору и терялся бы
                    # навсегда точно так же, как недостижимый. Ветка
                    # осталась без парковки в первой версии спринта 153, и
                    # нашла это мутационная проверка, промахнувшаяся мимо
                    # соседней ветки (спринт 153).
                    self._park(ref, str(exc))
                    self._advance_cursor(ref)
                    self._transient_fails.pop(ref.match_id, None)
                continue
            except Exception as exc:  # noqa: BLE001
                # Временный сбой: повторяем матч, но не бесконечно — иначе
                # ошибка, которую мы ошибочно сочли временной, встаёт
                # намертво поперёк очереди.
                if is_unreachable(exc):
                    # До хоста не достучались вовсе. Считаем это свойством
                    # ХОСТА, а не матча: следующий матч оттуда же ждёт та
                    # же судьба, и платить за неё второй раз незачем.
                    self._unreachable.record_failure(host)
                n = self._transient_fails.get(ref.match_id, 0) + 1
                self._transient_fails[ref.match_id] = n
                if n >= MAX_TRANSIENT_RETRIES:
                    # Курсор сдвигается — очередь важнее одного матча
                    # (инцидент 2026-07-31, 82 часа простоя). Но сам матч
                    # больше не пропадает: курсор монотонный и назад не
                    # ходит, а парковка помнит (спринт 153).
                    self._park(ref, str(exc))
                    logger.warning(
                        "match %s: download failed (%s), попытка %d/%d — "
                        "сдвигаю курсор и паркую матч, чтобы вернуться к "
                        "нему позже",
                        ref.match_id, exc, n, MAX_TRANSIENT_RETRIES)
                    self._advance_cursor(ref)
                    self._transient_fails.pop(ref.match_id, None)
                else:
                    logger.warning(
                        "match %s: download failed (%s), попытка %d/%d — "
                        "повторю следующим циклом",
                        ref.match_id, exc, n, MAX_TRANSIENT_RETRIES)
                continue
            self._transient_fails.pop(ref.match_id, None)
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
