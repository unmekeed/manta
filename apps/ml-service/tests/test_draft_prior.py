"""Тесты Draft Prior Model (трек F, F3).

Главное, что проверяется, — отсутствие утечки: winrate героев обязан
считаться без участия того матча, для которого потом строится фича.
Именно на этой ошибке модель показывала внутривыборочную корреляцию 0.9
при честном AUC 0.50.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.draft_prior import (BASE_FEATURES, PAIR_FEATURES, DraftRow,
                                  fit_stats, oof_priors, row_features,
                                  select_features, train)


def _rows(n: int, seed: int = 0) -> list[DraftRow]:
    """Синтетика с реальным сигналом: герой h0 сильный, h9 слабый."""
    rng = np.random.default_rng(seed)
    heroes = [f"npc_dota_hero_h{i}" for i in range(10)]
    out = []
    for i in range(n):
        r = list(rng.choice(heroes, 5, replace=False))
        d = list(rng.choice([h for h in heroes if h not in r], 5,
                            replace=False))
        p = 0.5 + 0.25 * (heroes[0] in r) - 0.25 * (heroes[0] in d)
        out.append(DraftRow(1000 + i, r, d, int(rng.random() < p)))
    return out


def test_select_features_gates_pairs_on_volume():
    # 1500 матчей / 127 героев: на пару приходится <1 наблюдения — только база.
    assert select_features(1500, 127) == BASE_FEATURES
    # Много матчей при том же числе героев — парные включаются.
    assert set(select_features(100_000, 127)) == set(BASE_FEATURES
                                                     + PAIR_FEATURES)


def test_smoothing_pulls_rare_hero_to_base():
    rows = _rows(200)
    st = fit_stats(rows)
    # Герой с одной игрой не должен получить winrate 0 или 1.
    st.hero_games["npc_dota_hero_rare"] = 1
    rare = st._smooth(1.0, 1.0)
    assert abs(rare - st.base) < 0.05, "редкий герой должен тянуться к базе"


def test_row_features_are_antisymmetric():
    """Зеркальный драфт обязан давать противоположные разности: модель
    не должна зависеть от того, какая сторона названа Radiant."""
    rows = _rows(300)
    st = fit_stats(rows)
    r = rows[0]
    mirrored = DraftRow(r.match_id, r.dire, r.radiant, 1 - r.win)
    a = row_features(r, st, 0, BASE_FEATURES)
    b = row_features(mirrored, st, 0, BASE_FEATURES)
    for i, name in enumerate(BASE_FEATURES):
        if name == "games_min":       # симметрична по построению
            assert a[i] == b[i]
        else:
            assert abs(a[i] + b[i]) < 1e-9, f"{name} не антисимметрична"


def test_train_returns_calibrated_artifact():
    art = train(_rows(900))
    assert set(art["features"]) <= set(BASE_FEATURES + PAIR_FEATURES)
    assert "booster" in art and "calibrator" in art
    m = art["metrics"]
    assert 0.0 <= m["brier"] <= 1.0
    assert m["train_matches"] > 0 and m["test_matches"] > 0
    assert m["heroes_known"] == 10


def test_oof_priors_cover_all_matches_and_are_probabilities():
    rows = _rows(400)
    priors = oof_priors(rows, folds=4)
    assert len(priors) == len(rows), "прайор нужен каждому матчу"
    assert all(0.0 <= p <= 1.0 for p in priors.values())


def test_oof_prior_does_not_memorise_outcome():
    """Ключевая проверка: на данных БЕЗ сигнала (исход — честная монетка,
    не зависящая от состава) OOF-прайор не должен коррелировать с
    исходом. Утечка проявилась бы именно здесь."""
    rng = np.random.default_rng(7)
    heroes = [f"npc_dota_hero_h{i}" for i in range(20)]
    rows = []
    for i in range(600):
        r = list(rng.choice(heroes, 5, replace=False))
        d = list(rng.choice([h for h in heroes if h not in r], 5,
                            replace=False))
        rows.append(DraftRow(2000 + i, r, d, int(rng.random() < 0.5)))

    priors = oof_priors(rows, folds=5)
    p = np.array([priors[r.match_id] for r in rows])
    y = np.array([r.win for r in rows], dtype=float)
    corr = abs(float(np.corrcoef(p, y)[0, 1])) if p.std() > 1e-9 else 0.0
    assert corr < 0.25, (
        f"OOF-прайор коррелирует с исходом на случайных данных ({corr:.2f}) "
        "— признак утечки состава в собственную фичу")


def test_train_learns_real_signal():
    """На синтетике с заложенным сигналом модель обязана быть лучше
    константы — иначе тренер сломан."""
    art = train(_rows(1200, seed=3))
    m = art["metrics"]
    assert m["brier"] <= m["brier_constant_baseline"], m
