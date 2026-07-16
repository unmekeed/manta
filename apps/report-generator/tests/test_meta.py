"""Тесты меты героев: сглаживание винрейта и сборка строк."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reportgen.meta import build_meta_rows, shrunk_winrate


def test_shrunk_winrate_pulls_small_samples_to_half():
    # 2/2 побед — сырой винрейт 100%, сглаженный далёк от него.
    assert shrunk_winrate(2, 2) == (2 + 5) / (2 + 10)  # ≈0.583
    # Большая выборка: сглаживание почти не влияет.
    assert abs(shrunk_winrate(600, 1000) - 0.6) < 0.002


def test_build_meta_rows():
    rows = build_meta_rows(
        [{"hero": "npc_dota_hero_axe", "matches": 10, "wins": 7,
          "avg_gpm": 512.3},
         {"hero": "", "matches": 5, "wins": 5, "avg_gpm": 1}],  # мусор
        total_matches=50)
    assert len(rows) == 1
    r = rows[0]
    assert r["hero"] == "npc_dota_hero_axe"
    assert r["winrate"] == 0.7
    assert r["pick_rate"] == 0.2
    assert r["shrunk_winrate"] == round((7 + 5) / 20, 4)  # 0.6


def test_build_meta_rows_empty_dataset():
    assert build_meta_rows([], 0) == []
