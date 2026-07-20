"""Death-Risk модель (Гл. 6.3, C5): P(смерть героя в ближайшие RISK_HORIZON_S).

Обучаемая замена эвристического Safety Index (0.65·давление + 0.35·глубина,
report-generator/builder.py): supervised-модель на реальных смертях из
реплеев. Сэмпл — снапшот живого героя из PositionSnapshots (~10 c шаг),
метка — погиб ли герой в (t, t+30] по KILL-событиям ReplayEvents.

Фичи позиционной обстановки момента t (RISK_FEATURES) сознательно
повторяют сигналы эвристики и добавляют контекст: модель сама выучивает
веса, которые в эвристике были подобраны руками. Разбиение — по матчам
(GroupShuffleSplit): сэмплы одного матча сильно скоррелированы, случайный
row-split дал бы утечку и дутый AUC.

Артефакт — тот же формат, что у Win Probability (booster-строка +
изотонический калибратор + features): сервится существующим классом
predictors.WinProbability без изменений.

CLI:  python -m training.risk [--max-matches N] [--out PATH] [--push]
"""
from __future__ import annotations

import argparse
import logging
import math
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import requests

logger = logging.getLogger("train-risk")

MODEL_NAME = "death_risk"
MODEL_VERSION = "0.1.0"
RISK_HORIZON_S = 30       # окно метки «умрёт в ближайшие N секунд»
POS_STALE_S = 45          # снапшот позиции старше — герой «не виден»
MAP_HALF_DIAG = 8000.0    # как в report-generator/builder.py
FAR = 20000.0             # «врагов не видно»: дальше любой реальной дистанции

RISK_FEATURES = [
    "game_time",
    "depth",              # глубина на чужой половине, 0..1
    "dist_nearest_enemy",
    "enemies_in_1500",
    "enemies_in_3000",
    "allies_in_1500",     # без самого героя
    "dist_nearest_ally",
    "alive_enemies",
    "alive_allies",
]


# -- выгрузка из ClickHouse ----------------------------------------------------

def _select_tsv(url: str, db: str, user: str, password: str,
                query: str) -> list[list[str]]:
    resp = requests.post(
        url, params={"database": db, "default_format": "TabSeparated"},
        data=query,
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        timeout=600)
    resp.raise_for_status()
    return [line.split("\t") for line in resp.text.splitlines()]


def _normalize_hero(name: str) -> str:
    for prefix in ("CDOTA_Unit_Hero_", "npc_dota_hero_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.replace("_", "").lower()


# -- построение сэмплов одного матча ------------------------------------------

def match_samples(positions: list[tuple[int, float, float, int, str]],
                  deaths: list[tuple[int, str]],
                  roster: dict[str, int],
                  horizon_s: int = RISK_HORIZON_S,
                  ) -> tuple[np.ndarray, np.ndarray]:
    """(t, x, y, is_alive, hero), (t_death, hero_npc), {hero_norm: team} → (X, y).

    player_id в PositionSnapshots не заполняется (нули) — герой и есть
    сущность; команда берётся из ростера PlayerMatchFeatures (team 2/3).
    Позиция героя на момент t — последний его снапшот <= t не старше
    POS_STALE_S (как _pos_at в report-generator). Иллюзии дают несколько
    строк героя на один тик — берётся последняя (шум бейзлайна).
    """
    by_hero: dict[str, list[tuple[int, float, float, int]]] = {}
    for t, x, y, alive, hero in positions:
        h = _normalize_hero(hero)
        if h in roster:                      # чужие сущности — мимо
            by_hero.setdefault(h, []).append((t, x, y, alive))
    # Дедуп по тику: герой + иллюзии дают несколько строк на один t, и без
    # дедупа моменты драк (где иллюзии живут) были бы пересэмплированы.
    for h, pts in by_hero.items():
        pts.sort()
        by_hero[h] = [p for i, p in enumerate(pts)
                      if i + 1 == len(pts) or pts[i + 1][0] != p[0]]

    death_ts: dict[str, list[int]] = {}
    for t, hero_npc in deaths:
        h = _normalize_hero(hero_npc)
        if h in by_hero:
            death_ts.setdefault(h, []).append(t)
    for ts in death_ts.values():
        ts.sort()

    times_by_hero = {h: [q[0] for q in pts] for h, pts in by_hero.items()}

    def pos_at(h: str, t: int) -> tuple[float, float, int] | None:
        pts = by_hero.get(h)
        if not pts:
            return None
        i = bisect_left(times_by_hero[h], t + 1) - 1
        if i < 0 or t - pts[i][0] > POS_STALE_S:
            return None
        _, x, y, alive = pts[i]
        return x, y, alive

    def dies_within(h: str, t: int) -> int:
        ts = death_ts.get(h)
        if not ts:
            return 0
        i = bisect_left(ts, t + 1)
        return 1 if i < len(ts) and ts[i] - t <= horizon_s else 0

    X, y = [], []
    heroes = sorted(by_hero)
    for hero in heroes:
        team_radiant = roster[hero] == 2
        enemies = [q for q in heroes if (roster[q] == 2) != team_radiant]
        allies = [q for q in heroes
                  if (roster[q] == 2) == team_radiant and q != hero]
        for t, x, ypos, alive in by_hero[hero]:
            if not alive:
                continue
            raw = (x + ypos) / (2.0 * MAP_HALF_DIAG)   # -1 (база R) .. +1 (база D)
            depth = (raw + 1.0) / 2.0 if team_radiant else (1.0 - raw) / 2.0
            depth = min(1.0, max(0.0, depth))

            d_enemy, e1500, e3000, alive_e = FAR, 0, 0, 0
            for q in enemies:
                p = pos_at(q, t)
                if p is None:
                    continue
                qx, qy, qalive = p
                if not qalive:
                    continue
                alive_e += 1
                d = math.hypot(qx - x, qy - ypos)
                d_enemy = min(d_enemy, d)
                e1500 += d <= 1500
                e3000 += d <= 3000

            d_ally, a1500, alive_a = FAR, 0, 0
            for q in allies:
                p = pos_at(q, t)
                if p is None:
                    continue
                qx, qy, qalive = p
                if not qalive:
                    continue
                alive_a += 1
                d = math.hypot(qx - x, qy - ypos)
                d_ally = min(d_ally, d)
                a1500 += d <= 1500

            X.append([t, depth, d_enemy, e1500, e3000, a1500, d_ally,
                      alive_e, alive_a])
            y.append(dies_within(hero, t))
    return (np.asarray(X, dtype=np.float32),
            np.asarray(y, dtype=np.int8))


# -- датасет по всем матчам ----------------------------------------------------

def load_dataset(url: str, db: str, user: str, password: str,
                 max_matches: int = 0, chunk: int = 100,
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(X, y, groups=match_id). Матчи: есть и позиции, и KILL-события
    (ReplayEvents под TTL — старые реплеи выпадают сами)."""
    rows = _select_tsv(url, db, user, password,
                       "SELECT DISTINCT match_id FROM PositionSnapshots "
                       "WHERE match_id IN (SELECT DISTINCT match_id "
                       "  FROM ReplayEvents WHERE event_type = 'KILL') "
                       "ORDER BY match_id DESC"
                       + (f" LIMIT {max_matches}" if max_matches else ""))
    match_ids = [int(r[0]) for r in rows]
    logger.info("матчей для датасета: %d", len(match_ids))

    xs, ys, gs = [], [], []
    for i in range(0, len(match_ids), chunk):
        ids = match_ids[i:i + chunk]
        id_list = ",".join(map(str, ids))
        pos = _select_tsv(url, db, user, password,
                          "SELECT match_id, game_time, x, y,"
                          "       is_alive, hero FROM PositionSnapshots"
                          f" WHERE match_id IN ({id_list})")
        kills = _select_tsv(url, db, user, password,
                            "SELECT match_id, game_time, target"
                            "  FROM ReplayEvents"
                            f" WHERE match_id IN ({id_list})"
                            "   AND event_type = 'KILL'"
                            "   AND target LIKE 'npc_dota_hero_%'")
        rosters_raw = _select_tsv(url, db, user, password,
                                  "SELECT match_id, hero, any(team)"
                                  "  FROM PlayerMatchFeatures FINAL"
                                  f" WHERE match_id IN ({id_list})"
                                  " GROUP BY match_id, hero")
        pos_by_match: dict[int, list] = {}
        for r in pos:
            pos_by_match.setdefault(int(r[0]), []).append(
                (int(r[1]), float(r[2]), float(r[3]), int(r[4]), r[5]))
        kills_by_match: dict[int, list] = {}
        for r in kills:
            kills_by_match.setdefault(int(r[0]), []).append(
                (int(r[1]), r[2]))
        roster_by_match: dict[int, dict[str, int]] = {}
        for r in rosters_raw:
            roster_by_match.setdefault(int(r[0]), {})[
                _normalize_hero(r[1])] = int(r[2])
        for mid in ids:
            roster = roster_by_match.get(mid, {})
            if (mid not in pos_by_match or mid not in kills_by_match
                    or len(roster) != 10):
                continue
            X, y = match_samples(pos_by_match[mid], kills_by_match[mid],
                                 roster)
            if len(X):
                xs.append(X)
                ys.append(y)
                gs.append(np.full(len(X), mid, dtype=np.int64))
        logger.info("обработано матчей: %d/%d", min(i + chunk, len(match_ids)),
                    len(match_ids))
    if not xs:
        return (np.empty((0, len(RISK_FEATURES)), dtype=np.float32),
                np.empty(0, dtype=np.int8), np.empty(0, dtype=np.int64))
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(gs)


# -- обучение ------------------------------------------------------------------

def train(X: np.ndarray, y: np.ndarray, groups: np.ndarray,
          seed: int = 42) -> dict:
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import (average_precision_score, brier_score_loss,
                                 roc_auc_score)
    from sklearn.model_selection import GroupShuffleSplit

    # 70/15/15 по матчам: обучение / калибровка+early-stop / честный тест.
    gss = GroupShuffleSplit(n_splits=1, test_size=0.30, random_state=seed)
    train_idx, rest_idx = next(gss.split(X, y, groups))
    gss2 = GroupShuffleSplit(n_splits=1, test_size=0.50, random_state=seed)
    calib_rel, test_rel = next(gss2.split(X[rest_idx], y[rest_idx],
                                          groups[rest_idx]))
    calib_idx, test_idx = rest_idx[calib_rel], rest_idx[test_rel]

    booster = lgb.train(
        {"objective": "binary", "learning_rate": 0.06, "num_leaves": 63,
         "min_child_samples": 200, "feature_fraction": 0.9,
         "bagging_fraction": 0.8, "bagging_freq": 1,
         "seed": seed, "verbose": -1},
        lgb.Dataset(X[train_idx], label=y[train_idx],
                    feature_name=RISK_FEATURES),
        num_boost_round=400,
        valid_sets=[lgb.Dataset(X[calib_idx], label=y[calib_idx])],
        callbacks=[lgb.early_stopping(30, verbose=False)])

    raw_calib = booster.predict(X[calib_idx])
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(raw_calib, y[calib_idx])

    raw_test = booster.predict(X[test_idx])
    p_test = calibrator.predict(raw_test)
    metrics = {
        "auc": round(float(roc_auc_score(y[test_idx], p_test)), 4),
        "pr_auc": round(float(average_precision_score(y[test_idx], p_test)), 4),
        "brier": round(float(brier_score_loss(y[test_idx], p_test)), 5),
        "base_rate": round(float(y[test_idx].mean()), 5),
        "test_rows": int(len(test_idx)),
        "best_iteration": int(booster.best_iteration or 0),
        "calibrator": "isotonic",
    }
    return {
        "booster": booster.model_to_string(),
        "calibrator": calibrator,
        "features": RISK_FEATURES,
        "metrics": metrics,
        "model_version": MODEL_VERSION,
        "algo": "lightgbm+isotonic",
        "dataset": {"matches": int(len(set(groups.tolist()))),
                    "rows": int(len(X)),
                    "positives": int(y.sum()),
                    "horizon_s": RISK_HORIZON_S},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


def push(artifact: dict, out_path: Path) -> tuple[str, bool]:
    """В реестр под именем death_risk; промоушен — если лучше prod по AUC
    на своём тесте (простой гейт: у модели нет общего holdout-механизма WP;
    честный сравнительный гейт — при появлении второй версии фич)."""
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
    ap.add_argument("--out", default="models/death_risk.pkl")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    X, y, groups = load_dataset(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"),
        max_matches=args.max_matches)
    if len(X) < 10_000:
        logger.error("слишком мало сэмплов: %d", len(X))
        return 1
    logger.info("датасет: %d сэмплов, %d матчей, положительных %.2f%%",
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
