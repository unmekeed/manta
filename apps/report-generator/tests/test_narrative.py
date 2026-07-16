"""Тесты LLM-нарратива: сборка фактов, деградация, интеграция с билдером."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reportgen.builder import build_analysis, build_timeline
from reportgen.narrative import LLMNarrator, _facts

PLAYERS = [
    {"player_id": 0, "hero": "npc_dota_hero_axe", "player_name": "A",
     "lane": "mid", "laning_score": 7.5, "impact_score": 8.0,
     "gpm": 512.0,
     "errors": [{"game_time": 600, "delta_wp": -0.12,
                 "type": "critical_death"}]},
    {"player_id": 5, "hero": "npc_dota_hero_kez", "player_name": "",
     "lane": "top", "laning_score": 3.0, "impact_score": 2.0,
     "gpm": 301.0, "errors": []},
]


def test_facts_contains_key_numbers():
    txt = _facts("Radiant", 0.83,
                 {"game_time": 1200, "delta_wp": 0.3}, PLAYERS)
    assert "Radiant" in txt and "0.83" in txt
    assert "минута 20" in txt and "+0.30" in txt
    assert "axe" in txt and "kez" in txt
    assert "критических смертей: 1" in txt
    assert "npc_dota_hero_" not in txt


def test_narrator_disabled_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    n = LLMNarrator()
    assert not n.enabled
    assert n.generate("Radiant", 0.8, None, PLAYERS) is None


def test_builder_falls_back_to_template():
    """narrator=None и narrator с отказом дают шаблон + source=template."""
    rows = [{"game_time": 60, "networth_diff": 100}]
    t = build_timeline(1, rows, [0.6])
    a = build_analysis(1, "Radiant", [
        {"player_id": 0, "hero": "npc_dota_hero_axe", "player_name": "A",
         "gpm": 500.0, "lh_at_10": 50, "dn_at_10": 5, "gold_share": 0.25},
    ], t, "0.1.0-x")
    assert a["narrative_source"] == "template"
    assert "Radiant" in a["narrative"]

    class Refusing:
        def generate(self, *args, **kwargs):
            return None

    a2 = build_analysis(1, "Radiant", [
        {"player_id": 0, "hero": "npc_dota_hero_axe", "player_name": "A",
         "gpm": 500.0, "lh_at_10": 50, "dn_at_10": 5, "gold_share": 0.25},
    ], t, "0.1.0-x", narrator=Refusing())
    assert a2["narrative_source"] == "template"


def test_builder_uses_llm_narrative():
    rows = [{"game_time": 60, "networth_diff": 100}]
    t = build_timeline(1, rows, [0.6])

    class Fake:
        def generate(self, winner, final_wp, turning, players):
            assert winner == "Radiant" and final_wp == 0.6
            assert players and "impact_score" in players[0]
            return "Разбор от LLM."

    a = build_analysis(1, "Radiant", [
        {"player_id": 0, "hero": "npc_dota_hero_axe", "player_name": "A",
         "gpm": 500.0, "lh_at_10": 50, "dn_at_10": 5, "gold_share": 0.25},
    ], t, "0.1.0-x", narrator=Fake())
    assert a["narrative"] == "Разбор от LLM."
    assert a["narrative_source"] == "llm"
    assert a["partial"] is True  # оценки всё ещё прокси
