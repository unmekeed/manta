"""Инвентаризация полей ответа OpenDota `/matches/{id}` (спринт 184).

ЗАЧЕМ. За один вызов OpenDota отдаёт 43 поля матча и 149 на игрока, а
читаем мы двадцать с небольшим. Остальное оплачено тем же вызовом и
просто выброшено — при том что квота была и остаётся самым дефицитным
ресурсом проекта. Понять, что именно выброшено, «посмотрев глазами»
нельзя: полей почти двести, и половина из них — служебный шум.

ФОРМА — та же, что у списка таблиц в спринте 156: поле обязано быть в
ОДНОМ из трёх списков, а незнакомое роняет тест. Так «мы про это поле не
подумали» перестаёт быть неотличимым от «мы его сознательно не берём».

  USED      — читается прямо сейчас.
  CANDIDATE — хотим взять, с указанием, что оно даст. Это ПЛАН РАБОТ,
              а не свалка: список коротким быть не обязан, но каждая
              строка — обещание.
  SKIPPED   — не берём, с причиной. Причина важнее самого факта:
              без неё через полгода никто не вспомнит, почему поле
              лежит здесь, и его либо возьмут зря, либо побоятся трогать.

Сверяется всё со СНЯТЫМ С ЖИВОГО API составом ответа
(tests/fixtures/opendota_match_fields.json): выдуманный по памяти
перечень — это ровно та ошибка, ради поимки которой модуль и написан.

    python -m collector.api_fields            # отчёт по фикстуре
    python -m collector.api_fields --json     # то же машинно
"""
from __future__ import annotations

import json
import pathlib

# -- матч ----------------------------------------------------------------------

MATCH_USED = {
    "match_id": "ключ везде",
    "duration": "длительность матча",
    "patch": "вес строки по возрасту патча (A9)",
    "radiant_win": "метка исхода — target обучения",
    "players": "всё поминутное живёт здесь",
    "objectives": "башни, бараки, Рошан, первая кровь (F2)",
    "picks_bans": "таблица драфта",
    "teamfights": "драки как события с исходом (спринт 186)",
}

MATCH_CANDIDATE = {
    "draft_timings": (
        "сколько думали над каждым пиком. Кандидат в трек G: спешка и "
        "долгие раздумья — поведение, а не механика"),
    "first_blood_time": "время первой крови; сейчас берём только сторону",
    "tower_status_radiant": "битовая маска башен на конец — сверка towers_diff",
    "tower_status_dire": "битовая маска башен Dire на конец матча",
    "barracks_status_radiant": "битовая маска бараков Radiant на конец",
    "barracks_status_dire": "битовая маска бараков Dire на конец",
    "lobby_type": "тип лобби: отделить рейтинговые от прочих",
    "game_mode": "режим: All Pick и Turbo — разные игры",
    "region": "регион сервера: мета различается",
    "leagueid": "лига — уточняет tier сверх нашего эвристического",
}

MATCH_SKIPPED = {
    "radiant_gold_adv": "сумма gold_t, которую мы и так считаем сами",
    "radiant_xp_adv": "сумма xp_t, которую мы и так считаем сами",
    "radiant_score": "итог матча, а модели нужен поминутный ряд",
    "dire_score": "итог матча; модели нужен поминутный ряд убийств",
    "cluster": "берётся из GC вместе с солью (ReplaySalts)",
    "replay_salt": "берётся из GC — на то и заведён спринт 171",
    "replay_url": "адрес собирается из соли, см. collector/salts.py",
    "chat": "переписка игроков — персональные данные, не собираем",
    "all_word_counts": "производное от чата",
    "my_word_counts": "производное от чата",
    "cosmetics": "предметы внешнего вида, на исход не влияют",
    "human_players": "почти всегда 10; вырожденная фича",
    "pauses": "паузы — редкость, и на поминутную динамику не ложатся",
    "throw": "производная OpenDota от исхода — утечка метки в признаки",
    "loss": "то же: считается ПОСЛЕ исхода",
    "pre_game_duration": "время до старта, одинаково у всех",
    "start_time": "время начала; свежесть выражена через match_id",
    "match_seq_num": "порядковый номер потока Valve, ведёт ranks scan",
    "series_id": "серия матчей — только для турнирных",
    "series_type": "формат серии (bo1/bo3) — только для турнирных",
    "engine": "версия движка Valve, служебное",
    "flags": "служебное поле OpenDota",
    "version": "версия парсера OpenDota, служебное",
    "metadata": "служебное поле OpenDota",
    "od_data": "служебное поле OpenDota",
}

# -- игрок ---------------------------------------------------------------------

PLAYER_USED = {
    "player_slot": "сторона игрока",
    "hero_id": "герой — пока только для таблицы драфта",
    "gold_t": "поминутное золото (F: экономика)",
    "xp_t": "поминутный опыт",
    "purchase_log": "закуп с таймстампами (F4)",
    "item_neutral": "нейтральный предмет (F6)",
    "buyback_log": "бэйбеки с таймстампами (F2)",
    "runes_log": "руны с таймстампами (F5)",
    "obs_log": "поставленные обсы (F5)",
    "obs_left_log": "снятые обсы — нужно для «активных сейчас»",
    "sen_log": "поставленные сентри (F5)",
    "sen_left_log": "снятые сентри",
    "lh_t": "поминутные добитки → lh_diff (спринт 185)",
    "dn_t": "поминутные денаи → dn_diff (спринт 185)",
    "hero_damage_t": "поминутный урон по героям → hero_damage_diff (185)",
    "hero_healing_t": "поминутное лечение → hero_healing_diff (185)",
    "camps_stacked_t": "поминутные стаки → camps_stacked_diff (185)",
    "rank_tier": "ранг игрока — кормит кэш рангов и отбор",
    "account_id": "ключ кэша рангов; в витрину не попадает",
}

PLAYER_CANDIDATE = {
    "kills_log": "убийства с таймстампами — точнее, чем objectives",
    "neutral_item_history": "смена нейтралок по времени (сейчас берём финальный)",
    "permanent_buffs": "постоянные баффы (мораль, аганимы) — растут по ходу",
    "lane": "номер линии, на которой играл герой",
    "lane_role": "роль на линии: контекст для всего остального",
    "is_roaming": "роумер меняет смысл линейной стадии",
    "teamfight_participation": "доля участия в драках",
    "stuns": "суммарное время станов — вклад в контроль",
    "pings": "пинги: поведенческий сигнал, трек G",
    "actions_per_min": "APM: поведенческий сигнал, трек G",
    "life_state_dead": "суммарное время мёртвым — оценка alive_diff для JSON-пути",
}

# Причины пропуска повторяются, поэтому собираются группами: писать одно
# и то же сто раз — верный способ развести формулировки и потерять смысл.
_SUMMARY = "итог матча, а модели нужен ПОМИНУТНЫЙ ряд"
_IDENTITY = "личность игрока — в витрину не попадает (псевдонимизация)"
_SERVICE = "служебное поле OpenDota"
_DERIVED = "производное от того, что уже считаем сами"
_FINAL_ITEMS = "итоговый инвентарь; по времени его даёт purchase_log"

PLAYER_SKIPPED = {
    **{k: _SUMMARY for k in (
        "kills", "deaths", "assists", "kda", "last_hits", "denies", "level",
        "net_worth", "total_gold", "total_xp", "gold_per_min", "xp_per_min",
        "kills_per_min", "hero_damage", "hero_healing", "tower_damage",
        "damage_taken", "damage", "damage_targets", "damage_inflictor",
        "damage_inflictor_received", "healing", "gold_spent", "gold",
        "hero_kills", "tower_kills", "towers_killed", "roshan_kills",
        "roshans_killed", "neutral_kills", "ancient_kills", "courier_kills",
        "observer_kills", "sentry_kills", "necronomicon_kills", "lane_kills",
        "hero_hits", "max_hero_hit", "multi_kills", "kill_streaks",
        "killed", "killed_by", "obs_placed", "sen_placed",
        "observers_placed", "camps_stacked", "creeps_stacked",
        "rune_pickups", "runes", "obs", "sen", "buyback_count",
        "lane_efficiency", "lane_efficiency_pct", "firstblood_claimed",
        "purchase_tpscroll", "purchase_ward_observer",
        "purchase_ward_sentry", "purchase_time", "first_purchase_time",
        "ability_uses", "ability_targets", "item_uses", "item_usage",
        "item_win", "observer_uses", "sentry_uses", "life_state",
        "gold_reasons", "xp_reasons", "benchmarks", "pred_vict",
        "computed_mmr", "position_est", "lane_pos", "neutral_tokens_log",
        "abandons", "leaver_status", "connection_log")},
    **{k: _IDENTITY for k in (
        "personaname", "name", "last_login",
        "is_subscriber", "is_contributor", "cosmetics", "party_id",
        "party_size", "randomed", "hero_variant")},
    **{k: _SERVICE for k in (
        "match_id", "start_time", "duration", "cluster", "region", "patch",
        "lobby_type", "game_mode", "radiant_win", "win", "lose",
        "team_number", "team_slot", "isRadiant")},
    **{k: _FINAL_ITEMS for k in (
        "item_0", "item_1", "item_2", "item_3", "item_4", "item_5",
        "backpack_0", "backpack_1", "backpack_2", "item_neutral2",
        "purchase", "moonshard", "aghanims_scepter", "aghanims_shard")},
    "ability_upgrades_arr": _DERIVED + ": уровни даёт levels_diff",
    "times": "сетка времени рядов; используется неявно вместе с gold_t",
    "actions": "разбивка действий по типам; агрегат берём как actions_per_min",
}

# src/collector/api_fields.py -> ... -> apps/data-collector/tests/fixtures
FIXTURE = (pathlib.Path(__file__).resolve().parents[2]
           / "tests" / "fixtures" / "opendota_match_fields.json")


def _classify(names, used, candidate, skipped):
    unknown = [n for n in names
               if n not in used and n not in candidate and n not in skipped]
    return {
        "используется": sorted(n for n in names if n in used),
        "кандидаты": sorted(n for n in names if n in candidate),
        "не берём": sorted(n for n in names if n in skipped),
        "НЕ РАЗОБРАНО": sorted(unknown),
    }


def inventory(fields: dict | None = None) -> dict:
    """Разбор состава ответа по трём спискам.

    `fields` — снимок имён (см. фикстуру). По умолчанию берётся он же:
    состав ответа меняется редко, а лишний вызов API стоит квоты.
    """
    if fields is None:
        fields = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {
        "матч": _classify(fields["match"], MATCH_USED, MATCH_CANDIDATE,
                          MATCH_SKIPPED),
        "игрок": _classify(fields["player"], PLAYER_USED, PLAYER_CANDIDATE,
                           PLAYER_SKIPPED),
    }


def main() -> int:
    import sys
    data = inventory()
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for scope, groups in data.items():
        print(f"\n== {scope}")
        for name, items in groups.items():
            print(f"  {name}: {len(items)}")
            if name in ("кандидаты", "НЕ РАЗОБРАНО") and items:
                src = (MATCH_CANDIDATE if scope == "матч" else PLAYER_CANDIDATE)
                for f in items:
                    why = src.get(f, "")
                    print(f"    • {f}" + (f" — {why}" if why else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
