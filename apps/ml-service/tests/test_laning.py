"""Тесты датасета Laning-модели (training.laning)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.laning import LANING_FEATURES, build_rows, train


def _player(mid, hero, lane, diff, lh=20, dn=3):
    return {"match_id": mid, "hero": hero, "lane": lane,
            "lane_nw_diff_at_10": diff, "lh_at_5": lh, "dn_at_5": dn}


def test_build_rows_labels_and_features():
    players = [
        _player(1, "npc_dota_hero_axe", "mid", 900, lh=30, dn=5),
        _player(1, "npc_dota_hero_kez", "mid", -900, lh=12, dn=1),
        _player(1, "npc_dota_hero_lion", "roam", 0),        # roam — мимо
        _player(1, "npc_dota_hero_pudge", "top", 0),        # нет диффа — мимо
    ]
    combat = {(1, "npc_dota_hero_axe"):
              {"dealt": 1500.0, "taken": 700.0, "kills": 1, "deaths": 0}}
    X, y, g = build_rows(players, combat)
    assert X.shape == (2, len(LANING_FEATURES))
    assert list(y) == [1, 0]                    # axe выиграл, kez проиграл
    assert list(g) == [1, 1]
    axe = X[0]
    assert axe[0] == 30 and axe[2] == 1500 and axe[4] == 1 and axe[6] == 1.0
    # kez без combat-строк — валидные нули, а не пропуск
    assert X[1][2] == 0 and X[1][5] == 0


def test_build_rows_empty_input():
    X, y, g = build_rows([], {})
    assert X.shape == (0, len(LANING_FEATURES)) and len(y) == 0 and len(g) == 0


def test_train_learns_separable_signal():
    """Синтетика: линию выигрывает тот, кто фармит и наносит урон.
    Модель должна разделить классы (AUC высокий) и вернуть артефакт
    в формате WinProbability (booster + calibrator + features)."""
    rng = np.random.default_rng(7)
    n = 3000
    win = rng.integers(0, 2, n)
    lh = rng.normal(35, 6, n) * win + rng.normal(15, 6, n) * (1 - win)
    dealt = rng.normal(2000, 400, n) * win + rng.normal(700, 400, n) * (1 - win)
    deaths = rng.poisson(0.2, n) * win + rng.poisson(1.2, n) * (1 - win)
    X = np.column_stack([
        np.clip(lh, 0, None),
        rng.poisson(3, n),
        np.clip(dealt, 0, None),
        rng.normal(1000, 300, n),
        rng.poisson(0.3, n),
        deaths,
        rng.integers(0, 2, n),
    ]).astype(np.float32)
    y = win.astype(np.int8)
    groups = (np.arange(n) // 10).astype(np.int64)   # 10 игроков на «матч»

    artifact = train(X, y, groups)
    assert artifact["metrics"]["auc"] > 0.8
    assert artifact["features"] == LANING_FEATURES
    assert "booster" in artifact and "calibrator" in artifact
    assert 0 < artifact["metrics"]["base_rate"] < 1
