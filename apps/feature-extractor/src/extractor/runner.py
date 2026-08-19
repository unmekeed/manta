"""Петля Feature Extractor (Гл. 3.5/3.6, спринт 9).

replay.parsed → чтение сырых таблиц ClickHouse → расчёт фич →
PlayerMatchFeatures + MatchTimelineFeatures → features.calculated.

Идемпотентность: витрины на ReplacingMergeTree(computed_at) — повторная
обработка того же матча замещает строки, at-least-once безопасен.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from confluent_kafka import Consumer, Producer
import manta_grpc
from prometheus_client import Counter, Histogram

from .clickhouse import ClickHouse
from .features import FEATURE_VERSION, Roster, player_features, timeline_features
from .fights import detect_fights
from .mapcells import build_cells_by_minute, core_heroes, phase_cells
from .timings import build_timings
from .pseudonym import apply as pseudonymize

logger = logging.getLogger("extractor")

# Метрики (Гл. 11.2.2): фичи — этап ETL-конвейера.
FEATURES_CALCULATED = Counter(
    "features_calculated_total", "Матчи с посчитанными фичами")
FEATURES_FAILED = Counter(
    "features_failed_total", "Сбои расчёта фич")
FEATURES_DURATION = Histogram(
    "features_duration_seconds", "Время расчёта фич матча",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10))

PRODUCER_NAME = "feature-extractor@0.1.0"
TOPIC_IN = "replay.parsed"
TOPIC_OUT = "features.calculated"


@dataclass
class ExtractorConfig:
    kafka_brokers: str = field(
        default_factory=lambda: os.getenv("KAFKA_BROKERS", "localhost:9092"))
    group_id: str = field(
        default_factory=lambda: os.getenv("KAFKA_GROUP_ID", "feature-extractor"))
    clickhouse_url: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    clickhouse_db: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_DB", "manta"))
    clickhouse_user: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_USER", "dota"))
    clickhouse_password: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    # Онлайн Feature Store (Гл. 3.6): пусто — запись выключена.
    feature_store_addr: str = field(
        default_factory=lambda: os.getenv("FEATURE_STORE_ADDR", ""))


def build_envelope(match_id: int, payload: dict, trace_id: str | None) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": TOPIC_OUT,
        "schema_version": "1.0.0",
        "trace_id": trace_id or uuid.uuid4().hex,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "producer": PRODUCER_NAME,
        "partition_key": f"match_id:{match_id}",
        "payload": payload,
    }


class Extractor:
    def __init__(self, cfg: ExtractorConfig):
        self.cfg = cfg
        self.ch = ClickHouse(cfg.clickhouse_url, cfg.clickhouse_db,
                             cfg.clickhouse_user, cfg.clickhouse_password)
        self.producer = Producer({"bootstrap.servers": cfg.kafka_brokers})
        self._fs_stub = None

    # -- онлайн Feature Store (Гл. 3.6) ---------------------------------------

    def _push_online(self, match_id: int, trows: list[dict]) -> None:
        """Последний timeline-срез матча → онлайн-слой (view match_timeline).

        Сбой стора не роняет обработку матча: онлайн-слой — кэш, истина
        в ClickHouse.
        """
        if not self.cfg.feature_store_addr or not trows:
            return
        try:
            from gen import services_pb2, services_pb2_grpc
            if self._fs_stub is None:
                chan = manta_grpc.channel(self.cfg.feature_store_addr, "feature-store")
                self._fs_stub = services_pb2_grpc.FeatureStoreStub(chan)
            last = trows[-1]
            vec = services_pb2.FeatureVector()
            vec.values["match_id"] = float(match_id)
            for k, v in last.items():
                if isinstance(v, (int, float)):
                    vec.values[k] = float(v)
            self._fs_stub.WriteFeatures(
                services_pb2.FeatureBatch(vectors=[vec],
                                          feature_view="match_timeline"),
                timeout=3)
        except Exception as e:  # noqa: BLE001 — best-effort кэш
            logger.warning("feature-store: запись не удалась (%s)", e)

    # -- обработка одного матча ------------------------------------------------

    def process_match(self, match_id: int, players: list[dict], winner: str,
                      duration_s: float, trace_id: str | None,
                      tier: str = "", patch: int = 0) -> dict:
        roster = Roster.from_players(players, winner)

        economy = self.ch.select(
            "SELECT player_id, game_time, net_worth, total_gold, total_xp,"
            "       lh, dn"
            "  FROM EconomyTimeline WHERE match_id = {match_id:UInt64}"
            " ORDER BY player_id, game_time",
            {"match_id": match_id})
        kills = self.ch.select(
            "SELECT game_time, target, attacker FROM ReplayEvents"
            " WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "   AND target LIKE 'npc_dota_hero_%'"
            " ORDER BY game_time",
            {"match_id": match_id})
        building_kills = self.ch.select(
            "SELECT game_time, target FROM ReplayEvents"
            " WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "   AND (target LIKE '%_tower%' OR target LIKE '%_rax_%')"
            " ORDER BY game_time",
            {"match_id": match_id})
        positions = self.ch.select(
            "SELECT game_time, hero, x, y, is_alive FROM PositionSnapshots"
            " WHERE match_id = {match_id:UInt64} ORDER BY game_time",
            {"match_id": match_id})
        if not economy:
            raise ValueError(f"no economy rows for match {match_id}")

        prows = player_features(economy, roster, duration_s,
                                positions=positions)
        trows = timeline_features(economy, kills, roster, positions=positions,
                                  building_kills=building_kills)
        for r in prows:
            r["match_id"] = match_id
            r["tier"] = tier
            # Гл. 9.7: псевдоним пишется всегда, ник — только в режиме
            # plain. Так витрина готова к переключению MANTA_PII_MODE без
            # перезаливки, а GDPR-поиск по хешу единообразен.
            pseudonymize(r)
        for r in trows:
            r["match_id"] = match_id
            r["tier"] = tier
            r["patch"] = patch

        # Драки пишем ДО витрин: ReplayEvents живёт 14 дней, и это
        # единственный шанс сохранить размен. Сбой здесь не должен
        # ронять обработку матча — витрина первична.
        # Инициализируем ДО try: карты ниже используют frows, и при сбое
        # детектора драк они получили бы NameError вместо честного
        # «драк нет» — то есть один сбой унёс бы два слоя данных.
        frows: list[dict] = []
        try:
            frows = detect_fights(kills, positions, roster.hero_team)
            for r in frows:
                r["match_id"] = match_id
            self.ch.insert_rows("MatchFights", frows)
        except Exception:  # noqa: BLE001
            logger.warning("матч %s: драки не сохранены", match_id,
                        exc_info=True)

        # Тепловые карты — по той же причине и в том же месте: сырьё
        # истекает по TTL, агрегат живёт. Отдельным запросом, потому что
        # kills выше берёт только смерти героев без координат.
        # Одним запросом на два агрегата: DAMAGE/HEAL намеренно НЕ берём —
        # их десятки тысяч на матч, а ни карте, ни таймингам они не нужны.
        raw_events = []
        try:
            raw_events = self.ch.select(
                "SELECT game_time, event_type, x, y,"
                "       attacker, target, inflictor, value_amount"
                "  FROM ReplayEvents"
                " WHERE match_id = {match_id:UInt64}"
                "   AND event_type IN ('KILL', 'WARD_PLACE', 'SMOKE',"
                "                      'ITEM_PURCHASE', 'ABILITY_CAST',"
                "                      'BUYBACK')"
                " ORDER BY game_time",
                {"match_id": match_id})
        except Exception:  # noqa: BLE001
            logger.warning("матч %s: события реплея не прочитаны", match_id,
                        exc_info=True)

        try:
            # Коры (позиции 1–3) ранжируются по добиткам из economy — той
            # же выборки, что прочитана выше. Отдельный запрос ради
            # ранжирования пяти чисел был бы лишним походом в ClickHouse
            # на каждый матч.
            # economy идёт в карты дважды и за разным: по ней ранжируются
            # коры (позиции 1–3) и по ней же определяется, в какие
            # интервалы герой фактически фармил. Отдельных запросов не
            # заводим — выборка уже прочитана выше.
            mrows = build_cells_by_minute(
                positions, raw_events, frows, roster.hero_team,
                core_heroes(economy, roster.teams, roster.heroes),
                economy=economy, heroes=roster.heroes)
            # Фазовые строки НЕ считаются заново — они СУММА поминутных.
            # Две независимые формулы для одной и той же карты разъехались
            # бы при первом же расхождении в фильтрах, и обе выглядели бы
            # правдоподобно (спринт 147).
            crows = phase_cells(mrows)
            for r in mrows:
                r["match_id"] = match_id
            for r in crows:
                r["match_id"] = match_id
            self._replace_rows("MatchMapCellsMinute", match_id, mrows)
            self._replace_rows("MatchMapCells", match_id, crows)
        except Exception:  # noqa: BLE001
            logger.warning("матч %s: карты не сохранены", match_id,
                        exc_info=True)

        # Тайминги предметов и способностей: до спринта 99 эти события
        # писались и умирали по TTL непрочитанными.
        try:
            hrows = build_timings(raw_events, roster.hero_team)
            for r in hrows:
                r["match_id"] = match_id
            self._replace_rows("MatchHeroTimings", match_id, hrows)
        except Exception:  # noqa: BLE001
            logger.warning("матч %s: тайминги не сохранены", match_id,
                        exc_info=True)

        self.ch.insert_rows("PlayerMatchFeatures", prows)
        self.ch.insert_rows("MatchTimelineFeatures", trows)
        self._push_online(match_id, trows)

        payload = {
            "match_id": match_id,
            "feature_version": FEATURE_VERSION,
            "player_rows": len(prows),
            "timeline_rows": len(trows),
        }
        env = build_envelope(match_id, payload, trace_id)
        self.producer.produce(TOPIC_OUT, key=env["partition_key"],
                              value=json.dumps(env).encode("utf-8"))
        self.producer.flush(10)
        logger.info("features calculated: match=%s players=%d timeline=%d",
                    match_id, len(prows), len(trows))
        return payload

    # -- идемпотентная запись агрегатов (спринт 106) ---------------------------

    def _replace_rows(self, table: str, match_id: int,
                      rows: list[dict]) -> None:
        """Записать строки матча, убрав прежние.

        Почему нельзя просто вставить. Обе таблицы — ReplacingMergeTree, и
        миграции обещают «переразбор матча ЗАМЕЩАЕТ строку». Это верно
        ровно до тех пор, пока пересчёт даёт ТЕ ЖЕ значения ключа
        сортировки, а в него входят hero/kind/name (тайминги) и координаты
        клетки (карты). Стоит поменяться справочнику предметов,
        нормализации героев или классификации — и строка получает НОВЫЙ
        ключ, ложась РЯДОМ со старой, а не поверх неё.

        Инцидент 2026-08-05: правка справочника предметов (спринт 105)
        оставила 8764 строки-призрака со старыми именами вида `item_49`.
        Заметить их нечем: агрегат выглядит полным, просто строк в нём
        больше, чем было событий, — а значит любой счётчик поверх него
        завышен, и завышен незаметно.

        Удаление СИНХРОННОЕ (`lightweight_deletes_sync = 2`): порядок
        «удалить, потом вставить» разваливается, если удаление применится
        позже вставки — маска по match_id снесёт и только что записанные
        строки. Полагаться на то, что дефолт и сегодня равен 2, нельзя.

        Пустой `rows` — не повод пропустить удаление: матч мог законно
        лишиться строк (например, все события оказались браком), и тогда
        старые обязаны уйти.
        """
        try:
            got = self.ch.select(
                f"SELECT count() AS n FROM {table}"    # noqa: S608 — имя
                " WHERE match_id = {match_id:UInt64}",  # из литералов кода
                {"match_id": match_id})
            existing = int(got[0]["n"]) if got else 0
        except Exception:  # noqa: BLE001
            # Не смогли посчитать — вставляем как раньше. Дубль хуже
            # пропуска данных, но потеря матча хуже дубля.
            logger.warning("матч %s: не проверил %s на старые строки",
                           match_id, table, exc_info=True)
            existing = 0
        if existing:
            self.ch.execute(
                f"DELETE FROM {table}"                  # noqa: S608
                " WHERE match_id = {match_id:UInt64}"
                " SETTINGS lightweight_deletes_sync = 2",
                {"match_id": match_id})
            logger.info("матч %s: %s — убрано прежних строк %d",
                        match_id, table, existing)
        self.ch.insert_rows(table, rows)

    # -- бэкфилл ---------------------------------------------------------------

    def backfill(self, match_ids: list[int] | None = None) -> int:
        """Пересчитать фичи существующих матчей (новая версия фич).

        Ростер восстанавливается из PlayerMatchFeatures; исход — из won.
        Витрины на ReplacingMergeTree — пересчёт замещает строки.
        """
        if match_ids is None:
            rows = self.ch.select(
                "SELECT DISTINCT match_id FROM MatchTimelineFeatures"
                " ORDER BY match_id")
            match_ids = [int(r["match_id"]) for r in rows]
        done = 0
        for mid in match_ids:
            # Любой сбой одного матча (в т.ч. транзиентный 503 ClickHouse
            # на подготовительных запросах) не прерывает весь бэкфилл.
            try:
                prows = self.ch.select(
                    "SELECT player_id, team, hero, player_name, won, duration_s"
                    "  FROM PlayerMatchFeatures FINAL"
                    " WHERE match_id = {match_id:UInt64} ORDER BY player_id",
                    {"match_id": mid})
                if not prows:
                    logger.warning("match %s: нет PlayerMatchFeatures, пропуск",
                                   mid)
                    continue
                players = [{"team": int(r["team"]), "name": r["player_name"],
                            "hero": r["hero"]} for r in prows]
                won_teams = {int(r["team"]) for r in prows if int(r["won"]) == 1}
                winner = "Radiant" if won_teams == {2} else "Dire"
                duration = float(prows[0].get("duration_s", 0))
                tier_rows = self.ch.select(
                    "SELECT any(tier) AS tier, any(patch) AS patch"
                    "  FROM MatchTimelineFeatures"
                    " WHERE match_id = {match_id:UInt64}", {"match_id": mid})
                tier = str(tier_rows[0]["tier"]) if tier_rows else ""
                patch = int(tier_rows[0].get("patch") or 0) if tier_rows else 0
                self.process_match(mid, players, winner, duration,
                                   trace_id=None, tier=tier, patch=patch)
                done += 1
            except Exception:  # noqa: BLE001
                logger.exception("backfill failed for match %s", mid)
        logger.info("backfill done: %d/%d matches", done, len(match_ids))
        return done

    # -- Kafka-петля -----------------------------------------------------------

    def run(self) -> None:
        consumer = Consumer({
            "bootstrap.servers": self.cfg.kafka_brokers,
            "group.id": self.cfg.group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            # Агрессивные таймауты: зависшее после сна/рестарта брокера
            # соединение должно рваться и пересоздаваться, а не молчать.
            "socket.keepalive.enable": True,
            "session.timeout.ms": 15000,
            "reconnect.backoff.max.ms": 5000,
        })
        consumer.subscribe([TOPIC_IN])
        metrics_port = int(os.getenv("METRICS_PORT", "9102"))
        if metrics_port:
            manta_grpc.serve_metrics(metrics_port, "feature-extractor")
        logger.info("feature-extractor started: brokers=%s topic=%s metrics=:%s",
                    self.cfg.kafka_brokers, TOPIC_IN, metrics_port)
        try:
            while True:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    logger.error("kafka error: %s", msg.error())
                    continue
                self._handle(msg.value())
                consumer.commit(msg)
        except KeyboardInterrupt:
            pass
        finally:
            consumer.close()

    def _handle(self, raw: bytes) -> None:
        try:
            env = json.loads(raw)
            payload = env.get("payload", {})
            match_id = int(payload["match_id"])
            players = payload.get("players") or []
            winner = payload.get("winner", "")
            duration_s = float(payload.get("duration_s", 0))
            tier = str(payload.get("tier", "") or "")
            patch = int(payload.get("patch") or 0)
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("bad replay.parsed event, skipping: %s", exc)
            return
        if not players:
            # Событие старой схемы (без ростера) — фичи посчитать нельзя.
            logger.warning("match %s: no roster in event, skipping", match_id)
            return
        try:
            with FEATURES_DURATION.time():
                self.process_match(match_id, players, winner, duration_s,
                                   env.get("trace_id"), tier=tier, patch=patch)
            FEATURES_CALCULATED.inc()
        except Exception:  # noqa: BLE001 — логируем и не блокируем партицию
            FEATURES_FAILED.inc()
            logger.exception("feature extraction failed for match %s", match_id)
