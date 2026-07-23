"""Обучение бейзлайна Win Probability (Гл. 6.2.2).

Стек по спецификации: LightGBM (binary) + калибратор поверх сырого выхода
(изотоническая регрессия на отложенных матчах). Метрика приёмки — Brier;
целевой порог из спецификации: ≤ 0.18 на реальных данных.

Запуск:
    python -m training.train_winprob [--synthetic N] [--out models/win_probability.pkl]

Реальные матчи читаются из MatchTimelineFeatures; пока их мало, добавка
--synthetic N дополняет датасет синтетикой (факт фиксируется в метаданных
артефакта — модель со синтетикой не должна попадать в прод).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss

from .dataset import (FEATURES, PRO_TIER, Dataset, dataset_hash,
                      load_from_clickhouse, merge, mirror_xy, synth_matches)
from .drift import compute_reference

logger = logging.getLogger("train_winprob")

MODEL_VERSION = "0.7.0"  # 0.7.0: networth_rel + фазовые Brier

# Монотонные ограничения — доменное знание (Гл. 6.2.2): вероятность
# победы Radiant не убывает по преимуществу в золоте/опыте/убийствах и
# территории; по времени и суммарным убийствам знак не фиксирован.
# Порядок соответствует dataset.FEATURES.
MONOTONE = {
    "game_time": 0,
    "networth_diff": 1,
    "xp_diff": 1,
    "kills_diff": 1,
    "kills_total": 0,
    "position_advance": 1,
    "alive_diff": 1,     # больше живых у Radiant → WP не убывает
    "towers_diff": 1,    # снесено больше зданий Dire → WP не убывает
    "rax_diff": 1,
    "networth_rel": 1,   # доля преимущества — та же монотонность, что у diff
}

LGB_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    # Гиперпараметры под режим «строк много, матчей мало»: снапшоты матча
    # скоррелированы, поэтому сложность дерева ограничиваем жёстче обычного —
    # листья не должны подгоняться под отдельные матчи.
    "num_leaves": 20,
    "min_data_in_leaf": 60,
    "lambda_l2": 2.0,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "monotone_constraints": [MONOTONE[f] for f in FEATURES],
    "verbose": -1,
    "seed": 42,
}

# Порог перехода изотоника → Platt: изотоника на «ступеньках» малой выборки
# переобучается, логистическая калибровка устойчивее.
PLATT_MAX_MATCHES = 50
N_FOLDS = 5


class _PlattCalibrator:
    """Platt scaling: логистическая регрессия на сыром скоре бустера.

    Интерфейс совместим с IsotonicRegression (predict → вероятности),
    сериализуется joblib'ом как часть артефакта.
    """

    def __init__(self):
        from sklearn.linear_model import LogisticRegression
        self._lr = LogisticRegression()

    def fit(self, raw: np.ndarray, y: np.ndarray) -> "_PlattCalibrator":
        self._lr.fit(np.asarray(raw).reshape(-1, 1), y)
        return self

    def predict(self, raw: np.ndarray) -> np.ndarray:
        return self._lr.predict_proba(np.asarray(raw).reshape(-1, 1))[:, 1]


def _match_weights(groups: np.ndarray) -> np.ndarray:
    """Вес строки 1/n_rows(match): каждый матч вносит одинаковый вклад в
    лосс — длинные матчи (60+ снапшотов) не доминируют над короткими."""
    _, inverse, counts = np.unique(groups, return_inverse=True,
                                   return_counts=True)
    return 1.0 / counts[inverse]


def train(ds: Dataset, num_rounds: int = 300, mirror: bool = True) -> dict:
    """Обучить модель + OOF-калибратор; вернуть артефакт с метаданными.

    Схема против двойного использования данных (малые датасеты):

    1. Датасет делится на train-часть и НЕТРОНУТУЮ валидацию (group split,
       фиксированный seed — гейт использует тот же holdout и кандидат его
       не видел НИ на одном этапе).
    2. Внутри train-части — K-fold OOF по матчам: K бустеров, каждый
       предсказывает свой отложенный фолд → out-of-fold предсказания на
       всю train-часть. Калибратор фитится ТОЛЬКО на OOF (бустер этих
       строк не видел) — калибровка не оптимистична.
    3. Финальный бустер обучается на всей train-части с числом раундов =
       медиана best_iteration фолдов (early stopping уже отработал в фолдах).
    4. Метрики: brier_oof — честная внутренняя оценка; brier_calibrated —
       на нетронутой валидации (метрика спеки и гейта).

    mirror=True: обучающие строки зеркалируются по сторонам (side-agnostic);
    OOF-предсказания, валидация и эталон — в исходной ориентации.
    Веса строк 1/n_rows(match) — вклад матча не зависит от его длины.
    """
    in_valid, pro = ds._valid_mask()
    tr_mask = ~in_valid & ~pro
    X_tr_all, y_tr_all = ds.X[tr_mask], ds.y[tr_mask]
    g_tr_all = ds.groups[tr_mask]
    X_va, y_va = ds.X[in_valid & ~pro], ds.y[in_valid & ~pro]
    # A9: даунвейт строк старого патча (dataset.patch_weights). Метрики
    # (valid/OOF/эталон) считаются БЕЗ этих весов — оцениваем на честной
    # популяции, даунвейт влияет только на то, чему модель учится.
    pw_tr_all = ds.patch_weights()[tr_mask]

    def _fit_booster(X, y, g, rounds, pw=None, valid=None):
        w = _match_weights(g)
        if pw is not None:
            w = w * pw
        if mirror:
            X, y = mirror_xy(X, y)
            w = np.concatenate([w, w])
        dtrain = lgb.Dataset(X, label=y, weight=w, feature_name=FEATURES)
        kwargs = {}
        if valid is not None:
            kwargs = {"valid_sets": [lgb.Dataset(valid[0], label=valid[1])],
                      "callbacks": [lgb.early_stopping(30, verbose=False)]}
        return lgb.train(LGB_PARAMS, dtrain, num_boost_round=rounds, **kwargs)

    # -- K-fold OOF по матчам train-части -------------------------------------
    matches = np.array(sorted(set(g_tr_all.tolist())))
    rng = np.random.default_rng(42)
    rng.shuffle(matches)
    folds = np.array_split(matches, min(N_FOLDS, max(2, len(matches))))
    oof_raw = np.full(len(y_tr_all), np.nan)
    best_iters = []
    for fold_matches in folds:
        va_f = np.isin(g_tr_all, fold_matches)
        tr_f = ~va_f
        if va_f.sum() == 0 or tr_f.sum() == 0:
            continue
        b = _fit_booster(X_tr_all[tr_f], y_tr_all[tr_f], g_tr_all[tr_f],
                         num_rounds, pw=pw_tr_all[tr_f],
                         valid=(X_tr_all[va_f], y_tr_all[va_f]))
        oof_raw[va_f] = b.predict(X_tr_all[va_f])
        best_iters.append(int(b.best_iteration or num_rounds))
    seen = ~np.isnan(oof_raw)

    # -- Калибратор на OOF (Гл. 6.2.2) ----------------------------------------
    n_tr_matches = len(matches)
    if n_tr_matches < PLATT_MAX_MATCHES:
        calibrator, calibrator_kind = _PlattCalibrator(), "platt"
    else:
        calibrator = IsotonicRegression(y_min=0.0, y_max=1.0,
                                        out_of_bounds="clip")
        calibrator_kind = "isotonic"
    calibrator.fit(oof_raw[seen], y_tr_all[seen])
    oof_cal = calibrator.predict(oof_raw[seen])

    # -- Финальный бустер на всей train-части ---------------------------------
    final_rounds = int(np.median(best_iters)) if best_iters else num_rounds
    booster = _fit_booster(X_tr_all, y_tr_all, g_tr_all, max(final_rounds, 1),
                           pw=pw_tr_all)

    raw_va = booster.predict(X_va)
    cal_va = calibrator.predict(raw_va)

    metrics = {
        "brier_oof": round(float(brier_score_loss(y_tr_all[seen], oof_cal)), 4),
        "brier_raw": round(float(brier_score_loss(y_va, raw_va)), 4),
        "brier_calibrated": round(float(brier_score_loss(y_va, cal_va)), 4),
        "logloss_calibrated": round(float(log_loss(y_va, np.clip(cal_va, 1e-6, 1 - 1e-6))), 4),
        "valid_rows": int(len(y_va)),
        "best_iteration": final_rounds,
        "calibrator": calibrator_kind,
        "oof_folds": len(best_iters),
    }
    # Фазовые Brier (Гл. 6.2.2): агрегат маскирует, что поздние минуты
    # тривиальны (WP → 0/1), а ранние — где модель реально слаба.
    t_va = X_va[:, 0]
    for name, lo, hi in (("early", 0, 600), ("mid", 600, 1500),
                         ("late", 1500, float("inf"))):
        m_ph = (t_va >= lo) & (t_va < hi)
        if m_ph.sum() > 0:
            metrics[f"brier_{name}"] = round(
                float(brier_score_loss(y_va[m_ph], cal_va[m_ph])), 4)

    # Эталон: матчи про-команд (tier=Professional) — вне train/valid.
    # Метрика на них показывает перенос модели, обученной на high-rank
    # пабликах, на тир-1 игру; в гейте продвижения НЕ участвует (пока
    # эталонная выборка мала), но фиксируется в каждой версии.
    X_bm, y_bm = ds.benchmark()
    if len(y_bm) > 0:
        cal_bm = calibrator.predict(booster.predict(X_bm))
        metrics["brier_benchmark_pro"] = round(
            float(brier_score_loss(y_bm, cal_bm)), 4)
        metrics["benchmark_rows"] = int(len(y_bm))
    # Референс распределения фич для PSI-дрейфа (Гл. 10.4): все строки,
    # которые модель видела (train+valid, исходная ориентация, без эталона).
    # auto.py сравнивает с ним текущую витрину и триггерит переобучение
    # при значимом дрейфе (типично — после баланс-патча).
    non_pro = (ds.tiers != PRO_TIER) if ds.tiers is not None \
        else np.ones(len(ds.y), dtype=bool)
    drift_reference = compute_reference(ds.X[non_pro], FEATURES)

    return {
        "model_version": MODEL_VERSION,
        "algo": f"lightgbm+oof-{calibrator_kind}+mirror",
        "features": FEATURES,
        "booster": booster.model_to_string(),
        "calibrator": calibrator,
        "metrics": metrics,
        "drift_reference": drift_reference,
        "dataset": {
            "matches": ds.n_matches,
            "synthetic_matches": ds.n_synthetic,
            "rows": int(len(ds.y)),
            "hash": dataset_hash(ds),
            # A9: параметры даунвейта старого патча в этой тренировке.
            "patch_latest": (int(ds.patches[ds.patches > 0].max())
                             if ds.patches is not None
                             and (ds.patches > 0).any() else 0),
            "patch_downweighted_rows": (int(np.sum(ds.patch_weights() < 1.0))),
            # Верхняя граница виденных матчей: гейт следующих поколений
            # сравнивает версии на матчах НОВЕЕ этой отметки — их не видел
            # ни prod (их ещё не было), ни кандидат (valid-сплит исключён).
            "max_match_id": int(ds.groups.max()) if len(ds.groups) else 0,
        },
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


MODEL_NAME = "win_probability"


def should_promote(new_metrics: dict, prod_metrics: dict | None) -> tuple[bool, str]:
    """Гейт по СОХРАНЁННЫМ метрикам (легаси-путь / нет доступа к данным).

    Используется как fallback, когда честное пересравнение на одних данных
    невозможно (нет датасета под рукой). Приоритет — Brier на про-эталоне;
    иначе валидационный Brier. ВНИМАНИЕ: валидации разных версий считаются на
    разных сплитах и строго несопоставимы — поэтому основной путь гейта теперь
    evaluate_gate() (пересчёт обеих моделей на общем holdout).
    """
    if prod_metrics is None:
        return True, "первая версия"
    new_bm = new_metrics.get("brier_benchmark_pro")
    prod_bm = prod_metrics.get("brier_benchmark_pro")
    if new_bm is not None and prod_bm is not None:
        if new_bm <= prod_bm:
            return True, f"benchmark {new_bm:.4f} <= prod {prod_bm:.4f}"
        return False, f"benchmark {new_bm:.4f} > prod {prod_bm:.4f}"
    if new_bm is not None and prod_bm is None:
        return True, ("у production нет эталонной метрики — "
                      "валидации несопоставимы, продвигаем оцененную на эталоне")
    # Эталона нет ни у кого — остаётся валидационный Brier.
    new_v = new_metrics.get("brier_calibrated", float("inf"))
    prod_v = prod_metrics.get("brier_calibrated", float("inf"))
    if new_v <= prod_v:
        return True, f"valid {new_v:.4f} <= prod {prod_v:.4f}"
    return False, f"valid {new_v:.4f} > prod {prod_v:.4f}"


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def predict_calibrated(model: dict, X: np.ndarray) -> np.ndarray:
    """Калиброванная WP по артефакту (booster-строка + изотоника).

    X режется до набора фич АРТЕФАКТА: FEATURES только дописываются в конец,
    поэтому старая модель (меньше фич) корректно оценивается на новой матрице
    — критично для честного гейта, где обе версии считаются на одних данных.
    """
    booster = lgb.Booster(model_str=model["booster"])
    n = len(model.get("features") or []) or X.shape[1]
    return model["calibrator"].predict(booster.predict(X[:, :n]))


def _paired_bootstrap_delta(y, p_new, p_prod, groups, n_boot: int = 200,
                            seed: int = 42) -> tuple[float, float]:
    """Bootstrap ПО МАТЧАМ разницы Brier(new) − Brier(prod).

    Ресэмплим матчи целиком (снапшоты одного матча скоррелированы), считаем
    Δ на каждой выборке — получаем точечную оценку и разброс (шум метрики на
    этом holdout). Возвращает (delta_point, delta_std).
    """
    delta_point = _brier(y, p_new) - _brier(y, p_prod)
    uniq = np.array(sorted(set(groups.tolist())))
    if len(uniq) < 3:
        return delta_point, 0.0
    idx_by_g = {int(g): np.where(groups == g)[0] for g in uniq}
    rng = np.random.default_rng(seed)
    deltas = []
    for _ in range(n_boot):
        sample = rng.choice(uniq, size=len(uniq), replace=True)
        rows = np.concatenate([idx_by_g[int(g)] for g in sample])
        deltas.append(_brier(y[rows], p_new[rows]) - _brier(y[rows], p_prod[rows]))
    return delta_point, float(np.std(deltas))


def evaluate_gate(new_art: dict, prod_art: dict, ds,
                  tol_floor: float = 0.0005) -> tuple[bool, str]:
    """Честный гейт: обе модели считаются на ОДНОМ holdout текущих данных.

    Убирает залипание на «удачном» маленьком prod-датасете — production
    пересчитывается на актуальной выборке, а не сравнивается по своей старой
    сохранённой метрике. Продвигаем, если кандидат НЕ ЗНАЧИМО хуже: Δ Brier в
    пределах шума (± bootstrap-σ по матчам). При равенстве в пределах шума
    предпочитаем новую версию — она обучена на бОльших данных и устойчивее.
    """
    X, y, groups, kind = ds.eval_holdout()
    # Матчи новее всего, что видела production, — идеальный holdout: их не
    # видел никто (prod — потому что их ещё не существовало, кандидат —
    # потому что валидация исключена из его обучения). Снимает смещение
    # переходного периода, когда prod старой схемы обучала калибратор на
    # общем valid-сплите и имела на нём нечестное преимущество.
    prod_max = (prod_art.get("dataset") or {}).get("max_match_id")
    if kind == "valid" and prod_max:
        fresh = groups > int(prod_max)
        if len(set(groups[fresh].tolist())) >= 30:
            X, y, groups = X[fresh], y[fresh], groups[fresh]
            kind = "fresh"
    if len(y) == 0:
        return True, "нет общего holdout — продвигаем"
    p_new = predict_calibrated(new_art, X)
    p_prod = predict_calibrated(prod_art, X)
    b_new, b_prod = _brier(y, p_new), _brier(y, p_prod)
    delta, std = _paired_bootstrap_delta(y, p_new, p_prod, groups)
    tol = max(tol_floor, std)
    ok = delta <= tol
    label = {"benchmark_pro": "про-эталон",
             "fresh": "свежие матчи (никто не видел)"}.get(kind, "валидация")
    n_m = len(set(groups.tolist()))
    verdict = "не хуже prod" if ok else "значимо хуже prod"
    reason = (f"{label}, одни данные ({n_m} матчей): new {b_new:.4f} vs "
              f"prod {b_prod:.4f} (Δ{delta:+.4f}, σ{std:.4f}) — {verdict}")
    return ok, reason


def push_with_gate(artifact: dict, out_path: Path, logger_, ds=None
                   ) -> tuple[str, bool, str]:
    """Загрузить версию в реестр; продвинуть через гейт.

    Если передан ds — гейт честный (evaluate_gate: обе модели на общем
    holdout текущих данных). Без ds — легаси-fallback по сохранённым метрикам
    (should_promote). Непродвинутая версия сохраняется в реестре.
    Возвращает (version, promoted, reason).
    """
    from registry import registry_from_env

    reg = registry_from_env()
    version = reg.push(MODEL_NAME, out_path.read_bytes(), {
        "model_version": artifact["model_version"],
        "algo": artifact["algo"],
        "features": artifact["features"],
        "metrics": artifact["metrics"],
        "dataset": artifact["dataset"],
        "trained_at": artifact["trained_at"],
        # компактный (≈1 КиБ) референс для PSI: auto.py читает его из
        # stage_metadata, не скачивая артефакт целиком
        "drift_reference": artifact.get("drift_reference", {}),
    })
    try:
        prod_bytes, _ = reg.resolve(MODEL_NAME, "production")
    except KeyError:
        prod_bytes = None

    if prod_bytes is None:
        ok, reason = True, "первая версия"
    elif ds is not None:
        prod_art = joblib.load(io.BytesIO(prod_bytes))
        ok, reason = evaluate_gate(artifact, prod_art, ds)
    else:
        prod = reg.stage_metadata(MODEL_NAME)
        ok, reason = should_promote(artifact["metrics"],
                                    prod.get("metrics") if prod else None)
    if ok:
        reg.promote(MODEL_NAME, version)
        logger_.info("registry: %s promoted (%s)", version, reason)
    else:
        logger_.warning("registry: %s NOT promoted (%s), версия сохранена",
                        version, reason)
    return version, ok, reason


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", type=int, default=0,
                    help="добавить N синтетических матчей (smoke-режим)")
    ap.add_argument("--min-matches", type=int, default=20)
    ap.add_argument("--push", action="store_true",
                    help="загрузить в реестр моделей (промоушен по Brier-гейту)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2]
                                         / "models" / "win_probability.pkl"))
    args = ap.parse_args()

    real = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    logger.info("real matches: %d (%d rows)", real.n_matches, len(real.y))

    ds = real
    if args.synthetic > 0:
        ds = merge(real, synth_matches(args.synthetic))
        logger.info("added %d synthetic matches", args.synthetic)

    if ds.n_matches < args.min_matches:
        logger.error("недостаточно матчей: %d < %d (добавьте --synthetic N "
                     "для smoke-прогона)", ds.n_matches, args.min_matches)
        return 1

    artifact = train(ds)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out)
    logger.info("metrics: %s", json.dumps(artifact["metrics"]))
    logger.info("artifact saved: %s (%.1f KiB)", out, out.stat().st_size / 1024)
    if args.push:
        push_with_gate(artifact, out, logger, ds=ds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
