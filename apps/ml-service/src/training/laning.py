"""Laning-модель (Гл. 6.2.1 Laning Evaluator, C5): P(линия выиграна к 10-й
минуте) по поведению игрока в первые 5 минут.

Обучаемая замена сигмоид-эвристики laning_score (report-generator/builder.py:
sigmoid(lane_nw_diff_at_10 / 1500)). Эвристика оценивает УЖЕ известный исход
линии; модель оценивает КАЧЕСТВО игры на линии: фичи берутся строго до 5-й
минуты (спека: «LH/DN на 5-й минуте, урон по оппоненту»), метка — исход
дуэли к 10-й (lane_nw_diff_at_10 > 0 из витрины спринта 20). Игрок, который
к 5-й минуте фармит, харасит и не умирает, получает высокий калиброванный
шанс выиграть линию — это и есть laning efficiency 0..1.

Сэмпл — игрок матча с определённой линией (top/mid/bot; roam без прямых
оппонентов — вне модели, там остаётся LH-фолбэк эвристики). Урон и
kills/deaths считаются по combat-логу ReplayEvents (только герой→герой),
поэтому датасет ограничен реплейными матчами внутри TTL — как у Death-Risk.

Разбиение — по матчам (GroupShuffleSplit): 10 игроков одного матча
скоррелированы (исходы линий связаны), row-split дал бы утечку.

Артефакт — формат Win Probability (booster-строка + изотоника + features):
сервится классом predictors.WinProbability без изменений,
Predict(model_name="laning").

CLI:  python -m training.laning [--max-matches N] [--out PATH] [--push]
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import requests

logger = logging.getLogger("train-laning")

MODEL_NAME = "laning"
MODEL_VERSION = "0.1.0"
LANING_WINDOW_S = 300     # фичи — только первые 5 минут игры
EARLY_FROM_S = -90        # пре-геймовые стычки (руны, блок лагеря) — тоже сигнал

LANING_FEATURES = [
    "lh_at_5",
    "dn_at_5",
    "hero_dmg_dealt",     # урон по вражеским героям за окно, суммарно
    "hero_dmg_taken",
    "kills_5",
    "deaths_5",
    "is_mid",             # мид — дуэль 1v1, боковые линии 2v2/2v3
]


def _select_tsv(url: str, db: str, user: str, password: str,
                query: str) -> list[list[str]]:
    resp = requests.post(
        url, params={"database": db, "default_format": "TabSeparated"},
        data=query,
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        timeout=600)
    resp.raise_for_status()
    return [line.split("\t") for line in resp.text.splitlines()]


# -- построение строк датасета -------------------------------------------------

def build_rows(players: list[dict], combat: dict[tuple[int, str], dict],
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """players (строки PlayerMatchFeatures) + combat[(match_id, hero)] →
    (X, y, groups). Игроки без определённой линии или с нулевым диффом
    (нет прямых оппонентов / старые данные до миграции 006) — мимо.

    Отсутствие героя в combat — валидные нули (пассивная 5-минутка без
    единого удара по герою реально бывает), а не пропуск данных: сам матч
    в датасет попадает только при наличии combat-лога (см. load_dataset).
    """
    X, y, g = [], [], []
    for p in players:
        lane = p["lane"]
        diff = int(p["lane_nw_diff_at_10"])
        if lane not in ("top", "mid", "bot") or diff == 0:
            continue
        c = combat.get((int(p["match_id"]), p["hero"]), {})
        X.append([
            float(p["lh_at_5"]),
            float(p["dn_at_5"]),
            float(c.get("dealt", 0)),
            float(c.get("taken", 0)),
            float(c.get("kills", 0)),
            float(c.get("deaths", 0)),
            1.0 if lane == "mid" else 0.0,
        ])
        y.append(1 if diff > 0 else 0)
        g.append(int(p["match_id"]))
    return (np.asarray(X, dtype=np.float32).reshape(-1, len(LANING_FEATURES)),
            np.asarray(y, dtype=np.int8),
            np.asarray(g, dtype=np.int64))


# Урон герой→герой и kills/deaths за лейнинг-окно, агрегировано по
# (match_id, hero) одним проходом. Урон по себе/иллюзиям с тем же именем
# отрезает attacker != target; урон по крипам/вышкам — фильтром на префикс.
COMBAT_QUERY = """
SELECT match_id, hero, sum(dealt), sum(taken), sum(kills), sum(deaths) FROM (
    SELECT match_id, attacker AS hero, value_amount AS dealt,
           0 AS taken, 0 AS kills, 0 AS deaths
      FROM ReplayEvents
     WHERE match_id IN ({ids}) AND event_type = 'DAMAGE'
       AND game_time BETWEEN {t0} AND {t1}
       AND attacker LIKE 'npc_dota_hero_%' AND target LIKE 'npc_dota_hero_%'
       AND attacker != target
    UNION ALL
    SELECT match_id, target AS hero, 0, value_amount, 0, 0
      FROM ReplayEvents
     WHERE match_id IN ({ids}) AND event_type = 'DAMAGE'
       AND game_time BETWEEN {t0} AND {t1}
       AND attacker LIKE 'npc_dota_hero_%' AND target LIKE 'npc_dota_hero_%'
       AND attacker != target
    UNION ALL
    SELECT match_id, attacker AS hero, 0, 0, 1, 0
      FROM ReplayEvents
     WHERE match_id IN ({ids}) AND event_type = 'KILL'
       AND game_time BETWEEN {t0} AND {t1}
       AND attacker LIKE 'npc_dota_hero_%' AND target LIKE 'npc_dota_hero_%'
    UNION ALL
    SELECT match_id, target AS hero, 0, 0, 0, 1
      FROM ReplayEvents
     WHERE match_id IN ({ids}) AND event_type = 'KILL'
       AND game_time BETWEEN {t0} AND {t1}
       AND target LIKE 'npc_dota_hero_%'
) GROUP BY match_id, hero
"""


def load_dataset(url: str, db: str, user: str, password: str,
                 max_matches: int = 0, chunk: int = 200,
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(X, y, groups=match_id). Матчи: реплейные (lane определён в витрине)
    И с живым combat-логом (ReplayEvents под TTL 14 дней)."""
    rows = _select_tsv(url, db, user, password,
                       "SELECT DISTINCT match_id FROM PlayerMatchFeatures FINAL"
                       " WHERE lane != '' AND match_id IN"
                       "   (SELECT DISTINCT match_id FROM ReplayEvents"
                       "     WHERE event_type = 'DAMAGE')"
                       " ORDER BY match_id DESC"
                       + (f" LIMIT {max_matches}" if max_matches else ""))
    match_ids = [int(r[0]) for r in rows]
    logger.info("матчей для датасета: %d", len(match_ids))

    xs, ys, gs = [], [], []
    for i in range(0, len(match_ids), chunk):
        ids = match_ids[i:i + chunk]
        id_list = ",".join(map(str, ids))
        players = [
            {"match_id": int(r[0]), "hero": r[1], "lane": r[2],
             "lane_nw_diff_at_10": int(r[3]),
             "lh_at_5": int(r[4]), "dn_at_5": int(r[5])}
            for r in _select_tsv(
                url, db, user, password,
                "SELECT match_id, hero, lane, lane_nw_diff_at_10,"
                "       lh_at_5, dn_at_5"
                "  FROM PlayerMatchFeatures FINAL"
                f" WHERE match_id IN ({id_list})")]
        combat = {
            (int(r[0]), r[1]): {"dealt": float(r[2]), "taken": float(r[3]),
                                "kills": int(r[4]), "deaths": int(r[5])}
            for r in _select_tsv(
                url, db, user, password,
                COMBAT_QUERY.format(ids=id_list, t0=EARLY_FROM_S,
                                    t1=LANING_WINDOW_S))}
        X, y, g = build_rows(players, combat)
        if len(X):
            xs.append(X)
            ys.append(y)
            gs.append(g)
        logger.info("обработано матчей: %d/%d", min(i + chunk, len(match_ids)),
                    len(match_ids))
    if not xs:
        return (np.empty((0, len(LANING_FEATURES)), dtype=np.float32),
                np.empty(0, dtype=np.int8), np.empty(0, dtype=np.int64))
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(gs)


# -- обучение ------------------------------------------------------------------

def train(X: np.ndarray, y: np.ndarray, groups: np.ndarray,
          seed: int = 42) -> dict:
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score
    from sklearn.model_selection import GroupShuffleSplit

    # 70/15/15 по матчам (см. risk.py — тот же контракт разбиения).
    gss = GroupShuffleSplit(n_splits=1, test_size=0.30, random_state=seed)
    train_idx, rest_idx = next(gss.split(X, y, groups))
    gss2 = GroupShuffleSplit(n_splits=1, test_size=0.50, random_state=seed)
    calib_rel, test_rel = next(gss2.split(X[rest_idx], y[rest_idx],
                                          groups[rest_idx]))
    calib_idx, test_idx = rest_idx[calib_rel], rest_idx[test_rel]

    # ~10 строк на матч (не тысячи, как у risk) — параметры малых данных.
    booster = lgb.train(
        {"objective": "binary", "learning_rate": 0.05, "num_leaves": 31,
         "min_child_samples": 40, "feature_fraction": 0.9,
         "bagging_fraction": 0.8, "bagging_freq": 1,
         "seed": seed, "verbose": -1},
        lgb.Dataset(X[train_idx], label=y[train_idx],
                    feature_name=LANING_FEATURES),
        num_boost_round=300,
        valid_sets=[lgb.Dataset(X[calib_idx], label=y[calib_idx])],
        callbacks=[lgb.early_stopping(30, verbose=False)])

    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(booster.predict(X[calib_idx]), y[calib_idx])

    p_test = calibrator.predict(booster.predict(X[test_idx]))
    brier = float(brier_score_loss(y[test_idx], p_test))
    base = float(y[test_idx].mean())
    metrics = {
        "auc": round(float(roc_auc_score(y[test_idx], p_test)), 4),
        "brier": round(brier, 5),
        # Спека (6.3.2) меряет Laning Evaluator в R²; для вероятностной
        # постановки аналог — brier skill score против базовой частоты
        # (доля дисперсии метки, объяснённая моделью).
        "brier_skill": round(1.0 - brier / (base * (1.0 - base)), 4),
        "base_rate": round(base, 5),
        "test_rows": int(len(test_idx)),
        "best_iteration": int(booster.best_iteration or 0),
        "calibrator": "isotonic",
    }
    return {
        "booster": booster.model_to_string(),
        "calibrator": calibrator,
        "features": LANING_FEATURES,
        "metrics": metrics,
        "model_version": MODEL_VERSION,
        "algo": "lightgbm+isotonic",
        "dataset": {"matches": int(len(set(groups.tolist()))),
                    "rows": int(len(X)),
                    "positives": int(y.sum()),
                    "window_s": LANING_WINDOW_S},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


def push(artifact: dict, out_path: Path) -> tuple[str, bool]:
    """В реестр под именем laning; промоушен — не хуже prod по AUC
    (тот же простой гейт, что у death_risk)."""
    from registry import registry_from_env

    reg = registry_from_env()
    version = reg.push(MODEL_NAME, out_path.read_bytes(), {
        k: artifact[k] for k in ("model_version", "algo", "features",
                                 "metrics", "dataset", "trained_at")})
    prod = reg.stage_metadata(MODEL_NAME)
    ok = (prod is None
          or artifact["metrics"]["auc"] >= prod["metrics"]["auc"] - 0.005)
    if ok:
        reg.promote(MODEL_NAME, version)
        logger.info("registry: %s promoted", version)
    else:
        logger.warning("registry: %s NOT promoted (AUC %.4f < prod %.4f)",
                       version, artifact["metrics"]["auc"],
                       prod["metrics"]["auc"])
    return version, ok


def main() -> int:
    import os
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-matches", type=int, default=0,
                    help="ограничить число матчей (0 — все)")
    ap.add_argument("--out", default="models/laning.pkl")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    X, y, groups = load_dataset(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"),
        max_matches=args.max_matches)
    if len(X) < 500:
        logger.error("слишком мало сэмплов: %d", len(X))
        return 1
    logger.info("датасет: %d игроков-строк, %d матчей, линия выиграна %.1f%%",
                len(X), len(set(groups.tolist())), 100 * y.mean())

    artifact = train(X, y, groups)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out)
    logger.info("metrics: %s", artifact["metrics"])
    if args.push:
        push(artifact, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
