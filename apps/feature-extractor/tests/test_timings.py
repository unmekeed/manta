"""Тайминги предметов и способностей (спринт 99).

Эти события писались в ReplayEvents с первого спринта и не читались
никем, а через 14 дней их стирал TTL. Тесты закрепляют то, что при
чтении сырья легко сделать наоборот и не заметить: кто именно актор
события и что считается его именем.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor import timings as timings_mod  # noqa: E402
from extractor.timings import build_timings  # noqa: E402

HERO_TEAM = {"npc_dota_hero_axe": 2, "npc_dota_hero_lina": 3}


def _ev(kind, t, attacker="", target="", inflictor="", value_amount=0):
    return {"event_type": kind, "game_time": t, "attacker": attacker,
            "target": target, "inflictor": inflictor,
            "value_amount": value_amount}


def _buy(t, item_id, hero="npc_dota_hero_axe"):
    """Покупка в том виде, в каком она приходит из ReplayEvents на живых
    данных: attacker и inflictor пусты, герой в target, предмет — числом.
    """
    return {"event_type": "ITEM_PURCHASE", "game_time": t, "attacker": "",
            "target": hero, "inflictor": "", "value_amount": item_id}


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


# -- покупки приходят числовым id, а не строкой (спринт 103) ------------------

def test_purchase_in_live_shape_is_not_dropped():
    """Главный дефект спринта 99: у покупки НЕТ строкового имени, и вся
    ветка молча возвращала пустую строку. За шесть часов так потерялись
    все 10164 события ITEM_PURCHASE — ровно то, ради чего спринт делался.
    """
    rows = build_timings([_buy(300, 41)], HERO_TEAM)
    assert len(rows) == 1, "покупка в живом формате снова отброшена"
    assert rows[0]["kind"] == "item" and rows[0]["name"] == "bottle"
    assert rows[0]["hero"] == "axe" and rows[0]["first_time"] == 300


def test_unknown_item_id_keeps_the_timing():
    """Справочник — снимок констант, он устареет с новым патчем. Тайминг
    покупки ценен и без имени, поэтому неизвестный id сохраняется, а не
    выбрасывается."""
    rows = build_timings([_buy(420, 987654)], HERO_TEAM)
    assert len(rows) == 1
    assert rows[0]["name"] == "item_987654"


def test_purchase_without_item_id_is_skipped():
    """Ноль/пусто в value — это не «предмет с id 0», а отсутствие данных.
    Записать его значило бы наплодить фантомный предмет `item_0`."""
    assert build_timings([_buy(300, 0)], HERO_TEAM) == []
    e = _buy(300, 0)
    del e["value_amount"]
    assert build_timings([e], HERO_TEAM) == []


def test_garbage_item_id_does_not_crash():
    e = _buy(300, 0)
    e["value_amount"] = "не число"
    assert build_timings([e], HERO_TEAM) == []


def test_string_item_name_still_works():
    """Запасной путь: форма события зависит от версии ядра-парсера, и
    строковая уже встречалась. Числовой id её вытесняет, но не отменяет.
    """
    rows = build_timings(
        [_ev("ITEM_PURCHASE", 300, attacker="npc_dota_hero_axe",
             inflictor="item_blink")], HERO_TEAM)
    assert rows[0]["name"] == "item_blink"


def test_item_id_wins_over_string_name():
    """Если пришли оба, доверяем id: строку ядро иногда заполняет именем
    героя или мусором, а id однозначен."""
    e = _buy(300, 41)
    e["inflictor"] = "item_blink"
    assert build_timings([e], HERO_TEAM)[0]["name"] == "bottle"


def test_item_dictionary_actually_loaded():
    """Справочник читается из файла в libs/data через путь с parents[4].
    Если файл переедет или путь съедет, except проглотит ошибку, словарь
    останется пустым — и покупки тихо превратятся в `item_<id>`. Внешне
    это выглядит рабочим: строки есть, агрегат заполняется.
    """
    assert len(timings_mod._ITEM_IDS) > 400, "справочник предметов не загружен"
    assert timings_mod._ITEM_IDS[1] == "blink"


def test_dictionary_includes_recipes():
    """Отдельно от проверки выше: справочник обязан быть снят с
    constants/item_ids, а не с constants/items. Второй ключуется по имени
    и рецептов не содержит — а рецепт это половина покупок в игре.
    Первый прогон 331 матча дал топ неразрешённых из одних рецептов
    (49 recipe_phase_boots — 510 раз). Проверка длины этого не ловит:
    501 запись против 596 выглядит правдоподобно.
    """
    assert timings_mod._ITEM_IDS[49] == "recipe_phase_boots"
    recipes = [n for n in timings_mod._ITEM_IDS.values()
               if n.startswith("recipe_")]
    assert len(recipes) > 50, f"рецептов в справочнике всего {len(recipes)}"
