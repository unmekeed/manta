"""Тесты датасета Death-Risk модели (training.risk)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.risk import RISK_FEATURES, match_samples


def _pos(t, x, y, hero, alive=1):
    return (t, float(x), float(y), alive, hero)


ROSTER = {"axe": 2, "kez": 3}


def test_labels_death_window_and_features():
    positions = [_pos(t, 4000, 4000, "CDOTA_Unit_Hero_Axe")
                 for t in (100, 110, 120, 200)]
    positions += [_pos(t, 4300, 4400, "CDOTA_Unit_Hero_Kez")
                  for t in (100, 110, 120, 200)]
    deaths = [(130, "npc_dota_hero_axe")]
    X, y = match_samples(positions, deaths, ROSTER)
    assert X.shape[1] == len(RISK_FEATURES)
    # сэмплы axe: t=100 (смерть через 30 — метка 1), 110, 120 → 1; 200 → 0
    axe = [(int(X[i][0]), int(y[i])) for i in range(len(y))
           if X[i][7] == 1]     # alive_enemies=1 только у axe (kez — враг)
    assert (100, 1) in axe and (110, 1) in axe and (200, 0) in axe
    # дистанция до врага ~500
    row = next(X[i] for i in range(len(y)) if X[i][0] == 100 and X[i][7] == 1)
    assert 490 < row[2] < 510 and row[3] == 1     # enemies_in_1500


def test_illusion_dedup_one_sample_per_tick():
    positions = [_pos(100, 4000, 4000, "CDOTA_Unit_Hero_Axe"),
                 _pos(100, 4100, 4100, "CDOTA_Unit_Hero_Axe"),  # иллюзия
                 _pos(100, 8000, 8000, "CDOTA_Unit_Hero_Kez")]
    X, y = match_samples(positions, [], ROSTER)
    # без дедупа было бы 3 сэмпла (axe ×2 + kez); с дедупом — по одному
    assert len(y) == 2


def test_dead_hero_not_sampled_and_unknown_ignored():
    positions = [_pos(100, 4000, 4000, "CDOTA_Unit_Hero_Axe", alive=0),
                 _pos(100, 5000, 5000, "CDOTA_Unit_Hero_Creep"),  # вне ростера
                 _pos(100, 8000, 8000, "CDOTA_Unit_Hero_Kez")]
    X, y = match_samples(positions, [], ROSTER)
    # только kez (axe мёртв, creep вне ростера)
    assert len(y) == 1 and X[0][8] == 0    # alive_allies у kez = 0
