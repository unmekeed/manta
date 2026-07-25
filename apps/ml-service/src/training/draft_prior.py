"""Draft Prior Model (трек F, спринт 62): P(победа Radiant | составы).

Зачем. Фазовый Brier WP: early ~0.19 против ~0.08 late. В первые минуты
экономика ещё ровная, и поминутная модель почти слепа — она стартует с
0.5 и ждёт, пока разойдётся net worth. Но исход частично предопределён
ДО первой минуты: составом. Прайор превращает старт кривой из «монетки»
в оценку букмекера, а дальше экономика его уточняет.

Схема каскада:

    draft_prior = P(win | составы, патч)      ← эта модель, 1 строка = матч
              ↓ как фича
    WP(t) = P(win | t, экономика, объективы, …, draft_prior)

Представление составов. Прямой one-hot по 126 героям на 5000 матчах
переобучится (в среднем ~40 матчей на героя за сторону). Поэтому фичи —
агрегатные и обучаемые НА СВОИХ ЖЕ данных:

  * winrate героя в датасете (сглаженный к 0.5 по числу игр — эмпирический
    Байес: у редкого героя оценка тянется к базовой);
  * сумма и максимум winrate состава, разница сторон;
  * пересечение с частыми парами (синергия) — усреднённый winrate пар
    внутри состава;
  * матчап-разница: средний winrate «герой против героя» по 25 парам.

Все агрегаты считаются ТОЛЬКО на train-части (иначе утечка исхода
валидационных матчей в фичи) — это ключевая деталь, ради которой модель
не сводится к «посчитать винрейты и сложить».

CLI:  python -m training.draft_prior [--min-matches N] [--out PATH] [--push]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import requests

logger = logging.getLogger("draft-prior")

MODEL_NAME = "draft_prior"
MODEL_VERSION = "0.1.0"

ALL_FEATURES = [
    "wr_sum_diff",      # Σ winrate состава R − D
    "wr_max_diff",      # лучший герой стороны
    "wr_min_diff",      # слабейший герой стороны
    "synergy_diff",     # средний winrate пар внутри состава
    "matchup_diff",     # средний winrate «наш герой против их героя»
    "games_min",        # надёжность оценок (мин. число игр среди 10 героев)
    "patch_age",        # возраст патча матча относительно свежего
]

# Базовый набор: только одногеройные winrate. Проверено на живых данных
# (1482 матча): пары и матчапы при таком объёме — генератор шума, а не
# сигнал. Пар из 127 героев ~8 тысяч, матчапов ~16 тысяч, то есть на
# комбинацию приходится около одного наблюдения: winrate пары фактически
# запоминает исход того же матча. Внутривыборочная корреляция с исходом
# доходила до 0.90, а честный AUC на отложенных матчах падал до 0.50
# (против 0.583 без этих фич). Включаются автоматически, когда данных
# хватит — см. select_features().
BASE_FEATURES = ["wr_sum_diff", "wr_max_diff", "wr_min_diff", "games_min"]
PAIR_FEATURES = ["synergy_diff", "matchup_diff"]

# Порог включения парных фич: сколько наблюдений в среднем должно
# приходиться на пару героев, чтобы её winrate перестал быть шумом.
PAIR_MIN_OBS = 8.0

PRIOR_SMOOTHING = 25.0   # игр, к которым тянется winrate редкого героя


def select_features(n_matches: int, n_heroes: int) -> list[str]:
    """Набор фич под объём данных: парные включаются, только когда на
    пару приходится хотя бы PAIR_MIN_OBS наблюдений."""
    if n_heroes < 2:
        return list(BASE_FEATURES)
    pairs = n_heroes * (n_heroes - 1) / 2
    obs_per_pair = (n_matches * 20) / pairs      # 2 состава × C(5,2)=10 пар
    if obs_per_pair >= PAIR_MIN_OBS:
        return BASE_FEATURES + PAIR_FEATURES
    return list(BASE_FEATURES)


@dataclass
class DraftRow:
    match_id: int
    radiant: list[str]
    dire: list[str]
    win: int
    patch: int = 0


@dataclass
class HeroStats:
    """Статистика героев/пар, посчитанная на train-части."""
    hero_wr: dict[str, float] = field(default_factory=dict)
    hero_games: dict[str, int] = field(default_factory=dict)
    pair_wr: dict[tuple[str, str], float] = field(default_factory=dict)
    matchup_wr: dict[tuple[str, str], float] = field(default_factory=dict)
    base: float = 0.5

    def _smooth(self, wins: float, games: float) -> float:
        return ((wins + PRIOR_SMOOTHING * self.base)
                / (games + PRIOR_SMOOTHING))


def fit_stats(rows: list[DraftRow]) -> HeroStats:
    """Собрать winrate героев, синергий и матчапов по train-матчам."""
    st = HeroStats()
    st.base = (sum(r.win for r in rows) / len(rows)) if rows else 0.5

    wins: dict[str, float] = {}
    games: dict[str, float] = {}
    pair_w: dict[tuple[str, str], float] = {}
    pair_g: dict[tuple[str, str], float] = {}
    mu_w: dict[tuple[str, str], float] = {}
    mu_g: dict[tuple[str, str], float] = {}

    for r in rows:
        for side, heroes in ((1, r.radiant), (0, r.dire)):
            won = r.win if side == 1 else 1 - r.win
            for h in heroes:
                wins[h] = wins.get(h, 0.0) + won
                games[h] = games.get(h, 0.0) + 1
            for i, a in enumerate(heroes):
                for b in heroes[i + 1:]:
                    k = (a, b) if a < b else (b, a)
                    pair_w[k] = pair_w.get(k, 0.0) + won
                    pair_g[k] = pair_g.get(k, 0.0) + 1
        for a in r.radiant:
            for b in r.dire:
                mu_w[(a, b)] = mu_w.get((a, b), 0.0) + r.win
                mu_g[(a, b)] = mu_g.get((a, b), 0.0) + 1

    st.hero_games = {h: int(g) for h, g in games.items()}
    st.hero_wr = {h: st._smooth(wins[h], games[h]) for h in games}
    st.pair_wr = {k: st._smooth(pair_w[k], pair_g[k]) for k in pair_g}
    st.matchup_wr = {k: st._smooth(mu_w[k], mu_g[k]) for k in mu_g}
    return st


def row_features(r: DraftRow, st: HeroStats, latest_patch: int,
                 features: list[str] | None = None) -> list[float]:
    def wr(h: str) -> float:
        return st.hero_wr.get(h, st.base)

    rw = [wr(h) for h in r.radiant]
    dw = [wr(h) for h in r.dire]

    def synergy(heroes: list[str]) -> float:
        vals = []
        for i, a in enumerate(heroes):
            for b in heroes[i + 1:]:
                k = (a, b) if a < b else (b, a)
                if k in st.pair_wr:
                    vals.append(st.pair_wr[k])
        return float(np.mean(vals)) if vals else st.base

    mu = [st.matchup_wr[(a, b)] for a in r.radiant for b in r.dire
          if (a, b) in st.matchup_wr]
    games = [st.hero_games.get(h, 0) for h in r.radiant + r.dire]

    values = {
        "wr_sum_diff": float(sum(rw) - sum(dw)),
        "wr_max_diff": float(max(rw) - max(dw)),
        "wr_min_diff": float(min(rw) - min(dw)),
        "synergy_diff": float(synergy(r.radiant) - synergy(r.dire)),
        "matchup_diff": float(np.mean(mu) - st.base) if mu else 0.0,
        "games_min": float(min(games)) if games else 0.0,
        "patch_age": float(max(0, latest_patch - r.patch)) if r.patch else 0.0,
    }
    return [values[f] for f in (features or BASE_FEATURES)]


# -- загрузка ------------------------------------------------------------------

def load_drafts(url: str, db: str, user: str, password: str,
                min_matches: int = 200) -> list[DraftRow]:
    """Драфты из MatchDraft; если она пуста — из витрины PlayerMatchFeatures
    (составы известны и для матчей, собранных до трека F)."""
    def q(sql: str) -> list[str]:
        r = requests.post(url, params={"database": db,
                                       "default_format": "JSONEachRow"},
                          data=sql,
                          headers={"X-ClickHouse-User": user,
                                   "X-ClickHouse-Key": password}, timeout=300)
        r.raise_for_status()
        return [l for l in r.text.splitlines() if l]

    rows: list[DraftRow] = []
    for line in q("SELECT match_id, radiant_heroes, dire_heroes, radiant_win,"
                  "       patch FROM MatchDraft FINAL"):
        d = json.loads(line)
        if len(d["radiant_heroes"]) == 5 and len(d["dire_heroes"]) == 5:
            rows.append(DraftRow(int(d["match_id"]), d["radiant_heroes"],
                                 d["dire_heroes"], int(d["radiant_win"]),
                                 int(d.get("patch") or 0)))
    if len(rows) >= min_matches:
        logger.info("драфтов из MatchDraft: %d", len(rows))
        return rows

    logger.info("MatchDraft мал (%d) — беру составы из PlayerMatchFeatures",
                len(rows))
    known = {r.match_id for r in rows}
    by_match: dict[int, dict] = {}
    for line in q("SELECT match_id, hero, team, won, any(patch) AS patch"
                  "  FROM (SELECT match_id, hero, team, won, 0 AS patch"
                  "          FROM PlayerMatchFeatures FINAL"
                  "         WHERE hero != '')"
                  " GROUP BY match_id, hero, team, won"):
        d = json.loads(line)
        mid = int(d["match_id"])
        if mid in known:
            continue
        e = by_match.setdefault(mid, {"r": [], "d": [], "win": 0})
        if int(d["team"]) == 2:
            e["r"].append(d["hero"])
            e["win"] = int(d["won"])
        else:
            e["d"].append(d["hero"])
    for mid, e in by_match.items():
        if len(e["r"]) == 5 and len(e["d"]) == 5:
            rows.append(DraftRow(mid, e["r"], e["d"], e["win"]))
    logger.info("драфтов всего: %d", len(rows))
    return rows


# -- обучение ------------------------------------------------------------------

def train(rows: list[DraftRow], seed: int = 42) -> dict:
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score

    rows = sorted(rows, key=lambda r: r.match_id)
    n = len(rows)
    # Сплит по match_id (время): валидация — САМЫЕ СВЕЖИЕ матчи. Так гейт
    # честен относительно смены меты, а не перемешивает эпохи.
    n_tr = int(n * 0.7)
    n_cal = int(n * 0.85)
    train_rows, calib_rows, test_rows = rows[:n_tr], rows[n_tr:n_cal], rows[n_cal:]

    # Статистика героев — ТОЛЬКО по train: иначе исход валидационного матча
    # просочится в его же фичи через winrate.
    st = fit_stats(train_rows)
    latest = max((r.patch for r in rows), default=0)
    features = select_features(len(train_rows), len(st.hero_wr))
    logger.info("набор фич под объём данных (%d матчей, %d героев): %s",
                len(train_rows), len(st.hero_wr), ", ".join(features))

    def mat(rs: list[DraftRow]):
        X = np.array([row_features(r, st, latest, features) for r in rs],
                     dtype=np.float64).reshape(-1, len(features))
        y = np.array([r.win for r in rs], dtype=np.int64)
        return X, y

    X_tr, y_tr = mat(train_rows)
    X_ca, y_ca = mat(calib_rows)
    X_te, y_te = mat(test_rows)

    booster = lgb.train(
        {"objective": "binary", "learning_rate": 0.03, "num_leaves": 7,
         "min_data_in_leaf": 60, "lambda_l2": 10.0, "feature_fraction": 0.9,
         "seed": seed, "verbose": -1},
        lgb.Dataset(X_tr, label=y_tr, feature_name=features),
        num_boost_round=400,
        valid_sets=[lgb.Dataset(X_ca, label=y_ca)],
        callbacks=[lgb.early_stopping(40, verbose=False)])

    calibrator = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    calibrator.fit(booster.predict(X_ca), y_ca)
    p_te = calibrator.predict(booster.predict(X_te))

    base_rate = float(y_te.mean()) if len(y_te) else 0.5
    brier = float(brier_score_loss(y_te, p_te)) if len(y_te) else float("nan")
    # Эталон — константный прогноз базовой частоты: прайор обязан быть лучше.
    brier_base = float(np.mean((y_te - y_tr.mean()) ** 2)) if len(y_te) else float("nan")
    metrics = {
        "brier": round(brier, 5),
        "brier_constant_baseline": round(brier_base, 5),
        "skill_vs_constant": round(1.0 - brier / brier_base, 4)
        if brier_base else 0.0,
        "auc": round(float(roc_auc_score(y_te, p_te)), 4)
        if len(set(y_te.tolist())) > 1 else 0.5,
        "base_rate": round(base_rate, 4),
        "train_matches": len(train_rows),
        "test_matches": len(test_rows),
        "heroes_known": len(st.hero_wr),
        "best_iteration": int(booster.best_iteration or 0),
    }
    return {
        "booster": booster.model_to_string(),
        "calibrator": calibrator,
        "features": features,
        "hero_stats": st,
        "latest_patch": latest,
        "metrics": metrics,
        "model_version": MODEL_VERSION,
        "algo": "lightgbm+isotonic",
        "dataset": {"matches": n},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


def predict_rows(artifact: dict, rows: list[DraftRow]) -> np.ndarray:
    """Прайор для списка драфтов (бэкфилл витрины)."""
    import lightgbm as lgb
    booster = lgb.Booster(model_str=artifact["booster"])
    st = artifact["hero_stats"]
    latest = artifact.get("latest_patch", 0)
    X = np.array([row_features(r, st, latest, artifact["features"])
                  for r in rows],
                 dtype=np.float64).reshape(-1, len(artifact["features"]))
    return artifact["calibrator"].predict(booster.predict(X))


def oof_priors(rows: list[DraftRow], folds: int = 5,
               seed: int = 42) -> dict[int, float]:
    """Out-of-fold прайоры для бэкфилла витрины.

    Прайор матча ОБЯЗАН считаться по статистике, в которую сам матч не
    входил: иначе его исход просачивается в собственную фичу через
    winrate героев, и WP переобучится на утечке. Поэтому — K-fold по
    матчам: для каждого фолда статистика и бустер учатся на остальных.
    """
    import lightgbm as lgb
    from sklearn.isotonic import IsotonicRegression

    rng = np.random.default_rng(seed)
    idx = np.arange(len(rows))
    rng.shuffle(idx)
    chunks = np.array_split(idx, folds)
    latest = max((r.patch for r in rows), default=0)
    out: dict[int, float] = {}

    for k, held in enumerate(chunks):
        held_set = set(held.tolist())
        fit_rows = [r for i, r in enumerate(rows) if i not in held_set]
        st = fit_stats(fit_rows)
        features = select_features(len(fit_rows), len(st.hero_wr))
        X_fit = np.array([row_features(r, st, latest, features)
                          for r in fit_rows], dtype=np.float64)
        y_fit = np.array([r.win for r in fit_rows], dtype=np.int64)
        booster = lgb.train(
            {"objective": "binary", "learning_rate": 0.03, "num_leaves": 7,
             "min_data_in_leaf": 60, "lambda_l2": 10.0, "seed": seed,
             "verbose": -1},
            lgb.Dataset(X_fit, label=y_fit, feature_name=features),
            num_boost_round=120)
        cal = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        cal.fit(booster.predict(X_fit), y_fit)

        held_rows = [rows[i] for i in held]
        X_h = np.array([row_features(r, st, latest, features)
                        for r in held_rows], dtype=np.float64)
        for r, p in zip(held_rows, cal.predict(booster.predict(X_h))):
            out[r.match_id] = float(p)
        logger.info("OOF фолд %d/%d: %d матчей", k + 1, folds, len(held_rows))
    return out


def backfill(rows: list[DraftRow], priors: dict[int, float], url: str, db: str,
             user: str, password: str) -> int:
    """Записать драфты и их прайоры в MatchDraft (TabSeparated)."""
    def esc(h: str) -> str:
        return "'" + h.replace("\\", "\\\\").replace("'", "\\'") + "'"

    lines = []
    for r in rows:
        lines.append("\t".join([
            str(r.match_id), str(r.patch), "", str(r.win),
            "[" + ",".join(esc(h) for h in r.radiant) + "]",
            "[" + ",".join(esc(h) for h in r.dire) + "]",
            "[]", "0", "backfill-mart",
            repr(priors.get(r.match_id, float("nan"))),
        ]))
    cols = ("match_id, patch, tier, radiant_win, radiant_heroes, dire_heroes,"
            " bans, first_pick_team, source, prior")
    resp = requests.post(
        url, params={"database": db,
                     "query": f"INSERT INTO MatchDraft ({cols}) "
                              f"FORMAT TabSeparated"},
        data=("\n".join(lines) + "\n").encode(),
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        timeout=300)
    resp.raise_for_status()
    return len(lines)


def push(artifact: dict, out_path: Path) -> tuple[str, bool]:
    from registry import registry_from_env

    reg = registry_from_env()
    version = reg.push(MODEL_NAME, out_path.read_bytes(), {
        k: artifact[k] for k in ("model_version", "algo", "features",
                                 "metrics", "dataset", "trained_at")})
    prod = reg.stage_metadata(MODEL_NAME)
    ok = (prod is None
          or artifact["metrics"]["brier"] <= prod["metrics"]["brier"] + 0.002)
    if ok:
        reg.promote(MODEL_NAME, version)
        logger.info("registry: %s promoted", version)
    else:
        logger.warning("registry: %s NOT promoted (Brier %.4f > prod %.4f)",
                       version, artifact["metrics"]["brier"],
                       prod["metrics"]["brier"])
    return version, ok


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-matches", type=int, default=300)
    ap.add_argument("--out", default="models/draft_prior.pkl")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="записать драфты и OOF-прайоры в MatchDraft")
    args = ap.parse_args()

    rows = load_drafts(os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
                       os.getenv("CLICKHOUSE_DB", "manta"),
                       os.getenv("CLICKHOUSE_USER", "dota"),
                       os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    if len(rows) < args.min_matches:
        logger.error("мало драфтов: %d < %d", len(rows), args.min_matches)
        return 1
    logger.info("датасет: %d матчей, winrate Radiant %.3f",
                len(rows), sum(r.win for r in rows) / len(rows))

    artifact = train(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out)
    logger.info("metrics: %s", artifact["metrics"])
    if args.push:
        push(artifact, out)
    if args.backfill:
        priors = oof_priors(rows)
        n = backfill(rows, priors,
                     os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
                     os.getenv("CLICKHOUSE_DB", "manta"),
                     os.getenv("CLICKHOUSE_USER", "dota"),
                     os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
        logger.info("MatchDraft: записано %d матчей с OOF-прайорами", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
