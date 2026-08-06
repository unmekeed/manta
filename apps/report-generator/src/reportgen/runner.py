"""Петля Report Generator (Гл. 3, спринт 15).

features.calculated → витрины ClickHouse → WP-кривая (gRPC MLService
PredictStream) → MatchReports (PostgreSQL, UPSERT) → report.generated.

Отчёт материализуется при генерации: путь чтения (шлюз) не трогает ни
ClickHouse, ни ML Service. Повторная доставка события перегенерирует
отчёт — UPSERT делает это идемпотентным.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import grpc

import manta_grpc
import psycopg
import requests
from confluent_kafka import Consumer, Producer
from prometheus_client import Counter, Histogram
from wp_rates import RATE_FEATURES, rates_for_row, window_columns

from . import retention
from .builder import build_analysis, build_timeline
from .gen import services_pb2, services_pb2_grpc

logger = logging.getLogger("reportgen")

REPORTS_GENERATED = Counter(
    "reports_generated_total", "Сгенерированные отчёты")
REPORTS_FAILED = Counter(
    "reports_failed_total", "Сбои генерации отчётов")
REPORTS_PURGED = Counter(
    "reports_purged_total", "Отчёты, удалённые по retention-политике")
REPORT_DURATION = Histogram(
    "report_duration_seconds", "Время генерации отчёта",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20))

PRODUCER_NAME = "report-generator@0.1.0"
TOPIC_IN = "features.calculated"
TOPIC_OUT = "report.generated"

# Фичи WP, которые берутся из витрины как есть (остальные — производные:
# kills_diff/kills_total из kills_radiant/kills_dire, networth_rel из
# networth_diff/networth_total). Порядок неважен — MLService принимает
# словарь по именам, — но ПОЛНОТА важна: сервер требует все имена из
# model.features и отвечает INVALID_ARGUMENT «missing features», если
# чего-то нет. Зеркало training.dataset.FEATURES из ml-service (импорта
# между сервисами нет); при добавлении фичи в модель дописывать сюда.
WP_PASSTHROUGH_FEATURES = [
    "position_advance", "alive_diff", "towers_diff", "rax_diff",
    # Трек F (миграция 012 + MatchDraft)
    "roshan_diff", "aegis_alive", "buybacks_diff", "first_blood",
    "item_value_diff", "key_items_diff", "obs_wards_diff", "sen_wards_diff",
    "runes_diff", "neutral_tier_diff", "levels_diff", "draft_prior",
    # Волна 1 (спринты 84, 90, 91). Их добавили в обучение, но забыли
    # здесь — тест test_wp_features_sync горел с тех самых пор. Пока в
    # production крутится модель постарше, Predict не спрашивает эти
    # фичи и всё выглядит рабочим; в день, когда гейт пропустит модель с
    # ними, отчёты начали бы падать с INVALID_ARGUMENT.
    "local_manpower_diff", "spread_diff", "vision_coverage_diff",
    "unspent_gold_diff", "buyback_availability",
]

# G1 (спринт 131): производные. Не колонки витрины и не локальный счёт —
# и SQL оконных колонок, и арифметика темпа берутся из общего
# libs/wp_rates.py. Свою копию здесь заводить нельзя: расхождение с
# ml-service не упало бы, а дало РАЗНЫЕ ЧИСЛА одной и той же модели в
# обучении и в проде — худший из возможных исходов.
WP_PASSTHROUGH_FEATURES += RATE_FEATURES

# Колонки витрины для _timeline_rows: сырьё под WP_PASSTHROUGH_FEATURES и
# производные. draft_prior живёт в MatchDraft — подтягивается джойном,
# производные считаются оконными функциями и колонками витрины не являются.
WP_ROW_COLUMNS = ["game_time", "networth_diff", "networth_total", "xp_diff",
                  "kills_radiant", "kills_dire"] + [
    f for f in WP_PASSTHROUGH_FEATURES
    if f != "draft_prior" and f not in RATE_FEATURES]


def _f(row: dict, key: str) -> float:
    """Отсутствующая фича (JSON-матчи, строки до миграции 008) — NaN.

    ClickHouse отдаёт NaN как null → None; NaN — корректный пропуск для
    модели, protobuf double его несёт.
    """
    v = row.get(key)
    return float(v) if v is not None else float("nan")


def wp_feature_values(row: dict) -> dict[str, float]:
    """Вектор фич WP по именам для gRPC MLService.

    Вынесено из замыкания внутри `_wp_curve` намеренно: это единственное
    место, где report-generator решает, что именно увидит модель, и оно
    обязано быть проверяемым тестом. Ровно на этом уже обжигались —
    забытые фичи волны 1 жили в коде спринтами и всплыли бы только в
    день, когда гейт продвинул бы модель с ними.

    Отдаются ВСЕ имена из model.features, включая те, которых у строки
    нет: сервер требует полный набор и отвечает INVALID_ARGUMENT на
    пропуск ключа. Отсутствующее значение — NaN, а не пропуск.
    """
    kills_r = float(row["kills_radiant"])
    kills_d = float(row["kills_dire"])
    total = _f(row, "networth_total")
    v = {"game_time": float(row["game_time"]),
         "networth_diff": float(row["networth_diff"]),
         "xp_diff": float(row["xp_diff"]),
         "kills_diff": kills_r - kills_d,
         "kills_total": kills_r + kills_d,
         "networth_rel": (float(row["networth_diff"]) / total
                          if total == total and total > 0 else float("nan"))}
    for name in WP_PASSTHROUGH_FEATURES:
        if name not in RATE_FEATURES:
            v[name] = _f(row, name)
    # G1: производные считаются общим кодом (libs/wp_rates.py) из оконных
    # колонок запроса — теми же формулами, что при обучении. Уровни, от
    # которых они берутся, к этому моменту уже в v.
    v.update(rates_for_row(row, v))
    return v


@dataclass
class ReportgenConfig:
    kafka_brokers: str = field(
        default_factory=lambda: os.getenv("KAFKA_BROKERS", "localhost:9092"))
    group_id: str = field(
        default_factory=lambda: os.getenv("KAFKA_GROUP_ID", "report-generator"))
    clickhouse_url: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_URL", "http://localhost:8123"))
    clickhouse_db: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_DB", "manta"))
    clickhouse_user: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_USER", "dota"))
    clickhouse_password: str = field(
        default_factory=lambda: os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    postgres_dsn: str = field(default_factory=lambda: os.getenv(
        "POSTGRES_DSN",
        "postgresql://dota:dota_dev_password@localhost:5432/manta"))
    ml_grpc_addr: str = field(
        default_factory=lambda: os.getenv("ML_GRPC_ADDR", "localhost:50051"))


class ReportGenerator:
    def __init__(self, cfg: ReportgenConfig):
        self.cfg = cfg
        self.db = psycopg.connect(cfg.postgres_dsn, autocommit=True)
        self.producer = Producer({"bootstrap.servers": cfg.kafka_brokers})
        self.ml = services_pb2_grpc.MLServiceStub(
            manta_grpc.channel(cfg.ml_grpc_addr, "ml-service"))
        # Retention: 0 — выключено (дефолт). Первая чистка — сразу после
        # старта, дальше раз в сутки.
        self._retention_days = retention.configured_days()
        self._last_purge = -86400.0

    # -- источники данных -------------------------------------------------------

    def _ch_select(self, query: str, match_id: int) -> list[dict]:
        resp = requests.post(
            self.cfg.clickhouse_url,
            params={"database": self.cfg.clickhouse_db,
                    "default_format": "JSONEachRow",
                    "param_match_id": str(match_id)},
            data=query,
            headers={"X-ClickHouse-User": self.cfg.clickhouse_user,
                     "X-ClickHouse-Key": self.cfg.clickhouse_password},
            timeout=60)
        resp.raise_for_status()
        return [json.loads(line) for line in resp.text.splitlines() if line]

    def _timeline_rows(self, match_id: int) -> list[dict]:
        cols = ", ".join(WP_ROW_COLUMNS)
        return self._ch_select(
            f"SELECT t.*, d.prior AS draft_prior"
            f"  FROM (SELECT match_id, {cols}, radiant_win,"
            f"               {window_columns()}"
            f"          FROM MatchTimelineFeatures FINAL"
            f"         WHERE match_id = {{match_id:UInt64}}) AS t"
            f"  LEFT JOIN (SELECT match_id, prior FROM MatchDraft FINAL) AS d"
            f"    USING (match_id)"
            f" ORDER BY game_time", match_id)

    def _kill_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT game_time, target, attacker FROM ReplayEvents"
            " WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "   AND target LIKE 'npc_dota_hero_%'"
            " ORDER BY game_time", match_id)

    def _map_cell_rows(self, match_id: int) -> list[dict]:
        """Клетки тепловых карт матча (спринт 110).

        FINAL обязателен: MatchMapCells — ReplacingMergeTree, и до
        слияния кусков переразобранный матч отдаёт клетки дважды. На
        карте это выглядело бы как удвоенная интенсивность в части
        клеток — правдоподобно и незаметно.
        """
        return self._ch_select(
            "SELECT phase, team, kind, gx, gy, n FROM MatchMapCells FINAL"
            " WHERE match_id = {match_id:UInt64}", match_id)

    def _position_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT game_time, hero, x, y, is_alive FROM PositionSnapshots"
            " WHERE match_id = {match_id:UInt64} ORDER BY game_time", match_id)

    def _model_fn(self, model_name: str, fallback_note: str):
        """Замыкание Predict(model_name=…) для опциональной extra-модели
        MLService. Модель не поднята (NOT_FOUND) — один warning и дальше
        None (вызывающий падает на эвристику); ошибки сети — то же."""
        state = {"disabled": False}

        def predict(feats: dict) -> float | None:
            if state["disabled"]:
                return None
            try:
                resp = self.ml.Predict(services_pb2.PredictRequest(
                    model_name=model_name,
                    features=services_pb2.FeatureVector(values=feats)),
                    timeout=5)
                return float(resp.win_probability_radiant)
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.NOT_FOUND:
                    logger.warning("%s не сервится — %s",
                                   model_name, fallback_note)
                else:
                    logger.warning("%s недоступна (%s) — %s", model_name,
                                   e.code().name, fallback_note)
                state["disabled"] = True
                return None

        return predict

    def _risk_fn(self):
        return self._model_fn("death_risk", "эвристический SI")

    def _laning_fn(self):
        return self._model_fn("laning", "эвристический laning_score")

    def _player_rows(self, match_id: int) -> list[dict]:
        return self._ch_select(
            "SELECT player_id, team, hero, player_name, player_hash,"
            "       won, gpm, xpm,"
            "       lh_at_5, dn_at_5, lh_at_10, dn_at_10, lane,"
            "       lane_nw_diff_at_10, gold_share"
            "  FROM PlayerMatchFeatures FINAL"
            " WHERE match_id = {match_id:UInt64} ORDER BY player_id", match_id)

    def _early_combat(self, match_id: int) -> dict[str, dict]:
        """hero → {dealt, taken, kills, deaths} за лейнинг-окно (фичи
        Laning-модели; тот же расчёт, что COMBAT_QUERY трейнера)."""
        rows = self._ch_select(
            "SELECT hero, sum(dealt) AS dealt, sum(taken) AS taken,"
            "       sum(kills) AS kills, sum(deaths) AS deaths FROM ("
            "  SELECT attacker AS hero, value_amount AS dealt,"
            "         0 AS taken, 0 AS kills, 0 AS deaths FROM ReplayEvents"
            "   WHERE match_id = {match_id:UInt64} AND event_type = 'DAMAGE'"
            "     AND game_time BETWEEN -90 AND 300"
            "     AND attacker LIKE 'npc_dota_hero_%'"
            "     AND target LIKE 'npc_dota_hero_%' AND attacker != target"
            "  UNION ALL"
            "  SELECT target, 0, value_amount, 0, 0 FROM ReplayEvents"
            "   WHERE match_id = {match_id:UInt64} AND event_type = 'DAMAGE'"
            "     AND game_time BETWEEN -90 AND 300"
            "     AND attacker LIKE 'npc_dota_hero_%'"
            "     AND target LIKE 'npc_dota_hero_%' AND attacker != target"
            "  UNION ALL"
            "  SELECT attacker, 0, 0, 1, 0 FROM ReplayEvents"
            "   WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "     AND game_time BETWEEN -90 AND 300"
            "     AND attacker LIKE 'npc_dota_hero_%'"
            "     AND target LIKE 'npc_dota_hero_%'"
            "  UNION ALL"
            "  SELECT target, 0, 0, 0, 1 FROM ReplayEvents"
            "   WHERE match_id = {match_id:UInt64} AND event_type = 'KILL'"
            "     AND game_time BETWEEN -90 AND 300"
            "     AND target LIKE 'npc_dota_hero_%'"
            ") GROUP BY hero", match_id)
        return {r["hero"]: {"dealt": float(r["dealt"]),
                            "taken": float(r["taken"]),
                            "kills": int(r["kills"]),
                            "deaths": int(r["deaths"])} for r in rows}

    def _wp_curve(self, match_id: int, rows: list[dict]
                  ) -> tuple[list[float], list[list[dict]], str]:
        def frames():
            for r in rows:
                yield services_pb2.FeatureFrame(
                    match_id=match_id, game_time=int(r["game_time"]),
                    features=services_pb2.FeatureVector(
                        values=wp_feature_values(r)))

        wp, drivers = [], []
        for p in self.ml.PredictStream(frames()):
            wp.append(p.radiant)
            drivers.append([{"feature": c.feature_name,
                             "value": round(c.contribution, 4)}
                            for c in p.top_contributions])
        # Версию модели узнаём отдельным Predict по последней точке.
        resp = self.ml.Predict(services_pb2.PredictRequest(
            match_id=match_id, model_name="win_probability",
            features=services_pb2.FeatureVector(
                values=wp_feature_values(rows[-1]))))
        return wp, drivers, resp.model_version

    # -- генерация ---------------------------------------------------------------

    def generate(self, match_id: int, feature_version: str = "",
                 trace_id: str | None = None) -> dict:
        rows = self._timeline_rows(match_id)
        if not rows:
            raise ValueError(f"no timeline features for match {match_id}")
        players = self._player_rows(match_id)
        winner = "Radiant" if int(rows[-1]["radiant_win"]) == 1 else "Dire"

        wp, drivers, model_version = self._wp_curve(match_id, rows)
        timeline = build_timeline(match_id, rows, wp, drivers)
        kills = self._kill_rows(match_id)
        positions = self._position_rows(match_id)
        analysis = build_analysis(match_id, winner, players, timeline,
                                  model_version, kills=kills,
                                  positions=positions,
                                  risk_fn=self._risk_fn(),
                                  laning_fn=self._laning_fn(),
                                  early_combat=self._early_combat(match_id),
                                  map_cells=self._map_cell_rows(match_id))

        self.db.execute(
            """INSERT INTO MatchReports
                   (match_id, analysis, timeline, model_version,
                    feature_version, generated_at)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON CONFLICT (match_id) DO UPDATE SET
                   analysis = EXCLUDED.analysis,
                   timeline = EXCLUDED.timeline,
                   model_version = EXCLUDED.model_version,
                   feature_version = EXCLUDED.feature_version,
                   generated_at = NOW()""",
            (match_id, json.dumps(analysis, ensure_ascii=False),
             json.dumps(timeline), model_version, feature_version))

        payload = {
            "match_id": match_id,
            "report_version": analysis["report_version"],
            "model_version": model_version,
            "summary": analysis["narrative"],
        }
        env = {
            "event_id": str(uuid.uuid4()),
            "event_type": TOPIC_OUT,
            "schema_version": "1.0.0",
            "trace_id": trace_id or uuid.uuid4().hex,
            "occurred_at": datetime.now(timezone.utc).isoformat()
                                   .replace("+00:00", "Z"),
            "producer": PRODUCER_NAME,
            "partition_key": f"match_id:{match_id}",
            "payload": payload,
        }
        self.producer.produce(TOPIC_OUT, key=env["partition_key"],
                              value=json.dumps(env, ensure_ascii=False).encode())
        self.producer.flush(10)
        logger.info("report generated: match=%s model=%s points=%d",
                    match_id, model_version, len(timeline["points"]))
        return payload

    # -- Kafka-петля ---------------------------------------------------------------

    def _maybe_purge(self) -> None:
        """Retention отчётов раз в сутки (Гл. 9.7, спринт 72).

        Живёт в петле report-generator, а не в cron: сервис и так
        единственный писатель MatchReports, отдельный планировщик — лишняя
        точка отказа. Выключено, пока не задан REPORTS_RETENTION_DAYS.
        Сбой чистки НЕ должен ронять генерацию отчётов — это гигиена
        хранилища, а не путь данных.
        """
        days = self._retention_days
        if days <= 0:
            return
        now = time.monotonic()
        if now - self._last_purge < 86400:
            return
        self._last_purge = now
        try:
            n = retention.purge(self.db, days, apply=True)
            if n:
                REPORTS_PURGED.inc(n)
        except Exception as e:  # noqa: BLE001 — гигиена не роняет конвейер
            logger.warning("retention: чистка не удалась (%s)", e)

    def run(self) -> None:
        consumer = Consumer({
            "bootstrap.servers": self.cfg.kafka_brokers,
            "group.id": self.cfg.group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "socket.keepalive.enable": True,
            "session.timeout.ms": 15000,
            "reconnect.backoff.max.ms": 5000,
        })
        consumer.subscribe([TOPIC_IN])
        metrics_port = int(os.getenv("METRICS_PORT", "9103"))
        if metrics_port:
            manta_grpc.serve_metrics(metrics_port, "report-generator")
        logger.info("report-generator started: brokers=%s topic=%s ml=%s metrics=:%s",
                    self.cfg.kafka_brokers, TOPIC_IN, self.cfg.ml_grpc_addr,
                    metrics_port)
        try:
            while True:
                self._maybe_purge()
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
            feature_version = str(payload.get("feature_version", ""))
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("bad features.calculated event, skipping: %s", exc)
            return
        try:
            with REPORT_DURATION.time():
                self.generate(match_id, feature_version, env.get("trace_id"))
            REPORTS_GENERATED.inc()
        except Exception:  # noqa: BLE001 — не блокируем партицию
            REPORTS_FAILED.inc()
            logger.exception("report generation failed for match %s", match_id)
