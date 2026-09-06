"""Раннер JSON-таймлайн источника: OpenDota → MatchTimelineFeatures напрямую.

В отличие от реплей-пути (S3 → Kafka → парсер → экстрактор) здесь конвейер
короткий: источник отдаёт готовые строки витрины, раннер пишет их в
ClickHouse и помечает матч в CollectedMatches (общий дедуп с реплей-путём:
один матч никогда не въезжает дважды, каким бы путём ни пришёл).

События features.calculated ПУБЛИКУЮТСЯ (спринт 195), и это изменение
прежнего решения. Раньше здесь стояло: «у JSON-матчей нет ReplayEvents и
позиций, полноценный отчёт по ним не собрать». Верно — но вывод из этого
был сделан неверный.

Отчётов в базе оказалось 520 при 3091 собранном матче: разбор получал
только реплейный путь, а JSON-матчи существовали исключительно ради
датасета. Для сайта это значит список, в котором пользователь своего
матча не найдёт.

ЧТО У JSON-МАТЧА ЕСТЬ: WP-кривая, счёт, длительность, патч, уровень,
драфт (MatchDraft заполняется этим же путём). Этого хватает и на карточку
списка, и на страницу таймлайна.

ЧЕГО НЕТ: поигрокового разреза (PlayerMatchFeatures пишет только
feature-extractor), тепловых карт, позиций, событий убийств — то есть
разбора ошибок и impact. Отчёт помечается как частичный, и потребитель
обязан РАЗЛИЧАТЬ «данных нет» и «данные нулевые»: пустой блок игроков без
пометки прочтётся как сломанный отчёт.

Вставка в ClickHouse — TabSeparated: текстовые nan корректно парсятся в
Float64 (JSONEachRow с null для не-Nullable колонки не прошёл бы).
"""
from __future__ import annotations

import json
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg
import requests
from confluent_kafka import Producer

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
               "levels_diff",
               # Волны 185–187 после ревизии (спринт 190): из двадцати
               # двух фич осталось семь. Колонки в ClickHouse не
               # удаляются — DROP необратим, а место копеечное; просто
               # перестаём их считать и читать (так же поступили с
               # draft_prior в спринте 134).
               "hero_damage_diff", "hero_healing_diff",
               "melee_diff", "attr_str_diff", "attr_agi_diff",
               "attr_int_diff", "attr_all_diff"]

# Фичи трека F и волны 1, которых может не быть (битый JSON) — пишем nan.
# Срез считается от конца: добавляя колонку в хвост MTF_COLUMNS, поправить
# и это число, иначе новая фича не попадёт в заполнение nan-ами.
F_TRACK_COLUMNS = MTF_COLUMNS[-21:]

DRAFT_COLUMNS = ["match_id", "patch", "tier", "radiant_win", "radiant_heroes",
                 "dire_heroes", "bans", "first_pick_team", "source"]
EVENT_COLUMNS = ["match_id", "game_time", "kind", "team", "player_slot",
                 "subtype", "x", "y"]


# Куда сообщать, что фичи матча готовы. Тот же топик, что у
# feature-extractor: потребитель (report-generator) не должен знать, каким
# путём приехал матч — иначе у него завелись бы две ветки на одно событие.
FEATURES_TOPIC = "features.calculated"
PRODUCER_NAME = "timeline-collector@1.0.0"
PUBLISH_TIMEOUT_S = 10.0


def build_features_envelope(match_id: int, feature_version: str,
                            timeline_rows: int) -> dict:
    """Конверт события по схеме Гл. 2.3.3.

    `player_rows: 0` — не забывчивость, а факт: поигрокового разреза у
    JSON-матча нет (его пишет только feature-extractor). Потребитель по
    этому нулю и понимает, что отчёт будет частичным.
    """
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": FEATURES_TOPIC,
        "schema_version": "1.0.0",
        "trace_id": uuid.uuid4().hex,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "producer": PRODUCER_NAME,
        "partition_key": f"match_id:{match_id}",
        "payload": {
            "match_id": match_id,
            "feature_version": feature_version,
            "player_rows": 0,
            "timeline_rows": timeline_rows,
        },
    }


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
        # Продюсер отключается пустым KAFKA_BROKERS: на стенде без Kafka
        # сбор обязан работать, просто без отчётов.
        brokers = os.getenv("KAFKA_BROKERS", "").strip()
        self._producer = Producer({"bootstrap.servers": brokers}) if brokers \
            else None

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
            # has_replay = FALSE и DO NOTHING: JSON-путь не должен
            # ПОНИЖАТЬ строку, поставленную реплейным путём. Матч с
            # реплеем полнее — у него есть позиции и события, — и
            # перезаписать его отметку значило бы отправить реплейный
            # источник качать заново то, что уже разобрано.
            cur.execute(
                """INSERT INTO CollectedMatches
                       (match_id, source_name, replay_url, has_replay)
                   VALUES (%s, %s, %s, FALSE)
                   ON CONFLICT (match_id) DO NOTHING""",
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
        # Драфт пишем ДО проверки raw: у STRATZ сырого JSON формы OpenDota
        # нет вовсе, но составы он отдаёт своим ответом и кладёт готовую
        # строку в tm.draft. До спринта 100 выход по `if not raw` уносил
        # вместе с фичами трека F и драфт — MatchDraft наполнялся только
        # матчами OpenDota, 1224 из 4129, и draft_prior оставался пустым.
        draft = getattr(tm, "draft", None)
        if draft:
            try:
                draft = {**draft, "tier": tm.tier, "patch": tm.patch}
                self._ch_insert("MatchDraft", DRAFT_COLUMNS, [draft])
            except Exception:  # noqa: BLE001 — витрина первична
                logger.warning("матч %d: драфт источника не записан",
                               tm.match_id, exc_info=True)

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
            self._announce(tm)
            processed += 1
            logger.info("таймлайн матча %d: %d строк (tier=%s)",
                        tm.match_id, len(tm.rows), tm.tier)
        return processed

    def _announce(self, tm) -> None:
        """Сказать конвейеру, что фичи матча готовы (спринт 195).

        ПОЧЕМУ НЕДОСТАВКА ЗДЕСЬ НЕ ОТМЕНЯЕТ СБОР — в отличие от
        реплейного пути, где непроверенная публикация стоила трёх суток
        (спринт 194). Там терялся ЕДИНСТВЕННЫЙ след: событие исчезало,
        матч помечался собранным, и вернуться к нему было неоткуда.

        Здесь главный продукт — строки витрины, и они уже записаны.
        Потерянное событие стоит лишь отсутствующего отчёта, а отчёт
        восстанавливается из витрины в любой момент. Поэтому матч
        остаётся собранным, а недоставка кричит в лог: чинить её надо,
        но откатывать сбор ради неё — значит переделывать дорогую работу
        из-за дешёвой потери.
        """
        if self._producer is None:
            return
        failures: list[str] = []

        def on_delivery(err, _msg):
            if err is not None:
                failures.append(str(err))

        env = build_features_envelope(tm.match_id, _feature_version(self._source),
                                      len(tm.rows))
        try:
            self._producer.produce(FEATURES_TOPIC,
                                   key=env["partition_key"].encode(),
                                   value=json.dumps(env).encode(),
                                   on_delivery=on_delivery)
            remaining = self._producer.flush(PUBLISH_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — Kafka недоступна
            remaining, failures = 1, [str(exc)]
        if remaining or failures:
            logger.error(
                "матч %d: событие %s НЕ доставлено (в очереди %s, причина: "
                "%s) — строки витрины записаны, но отчёт не будет собран. "
                "Проверь топики: make topics",
                tm.match_id, FEATURES_TOPIC, remaining,
                "; ".join(failures) or "таймаут")
