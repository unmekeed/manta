"""Тайминги предметов и способностей (спринт 99).

Эти события писались в ReplayEvents с первого спринта и не читались
никем, а через 14 дней их стирал TTL. Тесты закрепляют то, что при
чтении сырья легко сделать наоборот и не заметить: кто именно актор
события и что считается его именем.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor.timings import build_timings  # noqa: E402

HERO_TEAM = {"npc_dota_hero_axe": 2, "npc_dota_hero_lina": 3}


def _ev(kind, t, attacker="", target="", inflictor=""):
    return {"event_type": kind, "game_time": t, "attacker": attacker,
            "target": target, "inflictor": inflictor}


def test_actor_found_in_attacker():
    rows = build_timings(
        [_ev("ITEM_PURCHASE", 300, attacker="npc_dota_hero_axe",
             inflictor="item_blink")], HERO_TEAM)
    assert len(rows) == 1
    assert rows[0]["hero"] == "axe" and rows[0]["team"] == 2
    assert rows[0]["name"] == "item_blink"


def test_actor_found_in_target_when_attacker_empty():
    """В combat log имя актора лежит то в attacker, то в target — единого
    правила нет. Угадывать одно поле значило бы молча терять половину
    событий (или приписывать их не той стороне)."""
    rows = build_timings(
        [_ev("ITEM_PURCHASE", 300, target="npc_dota_hero_lina",
             inflictor="item_blink")], HERO_TEAM)
    assert len(rows) == 1
    assert rows[0]["hero"] == "lina" and rows[0]["team"] == 3


def test_hero_name_is_not_taken_as_item_name():
    """Имя предмета ищется в inflictor, потом в target. Без отсечения
    самого героя покупка записалась бы предметом «npc_dota_hero_axe»."""
    rows = build_timings(
        [_ev("ITEM_PURCHASE", 100, attacker="npc_dota_hero_axe",
             target="npc_dota_hero_axe", inflictor="")], HERO_TEAM)
    assert rows == []


def test_item_name_falls_back_to_target():
    rows = build_timings(
        [_ev("ITEM_PURCHASE", 100, attacker="npc_dota_hero_axe",
             target="item_tango")], HERO_TEAM)
    assert rows[0]["name"] == "item_tango"


def test_unresolved_names_are_dropped():
    """Неразрешённые по string table имена приходят как «#123»: имя не
    восстановить, а строка засорит агрегат."""
    rows = build_timings(
        [_ev("ABILITY_CAST", 100, attacker="npc_dota_hero_axe",
             inflictor="#4212")], HERO_TEAM)
    assert rows == []


def test_casts_counted_per_phase():
    evs = [_ev("ABILITY_CAST", t, attacker="npc_dota_hero_axe",
               inflictor="axe_culling_blade")
           for t in (100, 200, 800, 1600, 1700, 1800)]
    rows = build_timings(evs, HERO_TEAM)
    assert len(rows) == 1
    r = rows[0]
    assert (r["casts_early"], r["casts_mid"], r["casts_late"]) == (2, 1, 3)


def test_first_and_last_time_span_the_match():
    evs = [_ev("ABILITY_CAST", t, attacker="npc_dota_hero_axe",
               inflictor="axe_call") for t in (1500, 300, 900)]
    r = build_timings(evs, HERO_TEAM)[0]
    assert r["first_time"] == 300 and r["last_time"] == 1500


def test_first_time_does_not_depend_on_input_order():
    """Раннер сортирует события, но агрегат не должен на это полагаться:
    потерянный ORDER BY в запросе тихо испортил бы все тайминги."""
    ordered = build_timings(
        [_ev("ITEM_PURCHASE", 200, attacker="npc_dota_hero_axe",
             inflictor="item_blink"),
         _ev("ITEM_PURCHASE", 900, attacker="npc_dota_hero_axe",
             inflictor="item_blink")], HERO_TEAM)
    reversed_ = build_timings(
        [_ev("ITEM_PURCHASE", 900, attacker="npc_dota_hero_axe",
             inflictor="item_blink"),
         _ev("ITEM_PURCHASE", 200, attacker="npc_dota_hero_axe",
             inflictor="item_blink")], HERO_TEAM)
    assert ordered == reversed_
    assert ordered[0]["first_time"] == 200


def test_buyback_has_fixed_name():
    rows = build_timings(
        [_ev("BUYBACK", 2000, attacker="npc_dota_hero_lina")], HERO_TEAM)
    assert rows[0]["kind"] == "buyback" and rows[0]["name"] == "buyback"
    assert rows[0]["casts_late"] == 1


def test_non_hero_actors_are_skipped():
    """Крипы и башни кастуют и покупают тоже — в тайминги героев им
    нельзя, иначе агрегат распухнет мусором."""
    rows = build_timings(
        [_ev("ABILITY_CAST", 100, attacker="npc_dota_creep_badguys_melee",
             inflictor="some_ability")], HERO_TEAM)
    assert rows == []


def test_irrelevant_event_types_ignored():
    rows = build_timings(
        [_ev("DAMAGE", 100, attacker="npc_dota_hero_axe", inflictor="x"),
         _ev("KILL", 100, attacker="npc_dota_hero_axe",
             target="npc_dota_hero_lina")], HERO_TEAM)
    assert rows == []


def test_rows_carry_no_match_id():
    rows = build_timings(
        [_ev("BUYBACK", 100, attacker="npc_dota_hero_axe")], HERO_TEAM)
    assert rows and "match_id" not in rows[0]
