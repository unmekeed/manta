"""Тайминги предметов и способностей по героям (спринт 99).

Мотив — тот же TTL, что у драк и карт, но здесь потеря была полной:
ITEM_PURCHASE, ABILITY_CAST и BUYBACK писались в ReplayEvents с первого
спринта и НЕ ЧИТАЛИСЬ никем. Через 14 дней их стирало, а `.dem` к тому
моменту давно удалён.

Что даёт агрегат: тайминг первой покупки ключевого предмета
(`power_spike_diff`) и то, сколько раз сторона применяла способность в
каждой фазе (`ult_availability_diff`, ценность 5/5 по каталогу).

Про определение действующего героя. В combat log имя актора лежит то в
attacker, то в target — в зависимости от типа события, и однозначного
правила «всегда attacker» нет. Угадывать здесь опасно: перепутанные
стороны дадут правдоподобный, но зеркальный результат, и заметить это
почти нечем (ровно так уже обожглись с картой смертей). Поэтому актор
определяется ПО ФАКТУ: берётся то из полей, что оказалось известным
героем матча. Если оба — предпочитается attacker.
"""
from __future__ import annotations

import json
from pathlib import Path

from .features import _normalize_hero
from .mapcells import phase_of

# Снимок constants/item_ids OpenDota: id → npc-имя предмета. Нужен потому,
# что у события покупки в combat log НЕТ строкового имени — там пусто в
# inflictor, герой в target, а сам предмет лежит числовым id в value
# (проверено на живых данных 2026-08-04: 32 talisman_of_evasion,
# 41 bottle, 45 courier). Первая версия спринта 99 искала имя в строках
# и молча выбрасывала ВСЕ покупки — ровно то, ради чего спринт делался.
#
# Источник именно item_ids, а НЕ constants/items: второй ключуется по
# имени и рецептов не содержит вовсе. На первом прогоне 331 матча это
# дало десятки тысяч безымянных покупок — топ неразрешённых оказался
# сплошь рецептами (49 recipe_phase_boots — 510 раз, 167 recipe_desolator,
# 165 recipe_maelstrom). Рецепт — половина всех покупок в игре.
#
# Дыры в самой константе остаются (333, 341, 352 отсутствуют и в
# item_ids, и в снимке dotaconstants на GitHub) — их и обслуживает
# запасное имя `item_<id>`.
_ITEM_IDS: dict[int, str] = {}
try:
    _raw = json.loads(
        (Path(__file__).resolve().parents[4] / "libs" / "data"
         / "item_ids.json").read_text(encoding="utf-8"))
    _ITEM_IDS = {int(k): v for k, v in _raw.items()}
except Exception:  # noqa: BLE001 — словарь опционален, id сохраним как есть
    pass

# Типы событий ReplayEvents → вид записи в MatchHeroTimings.
KINDS = {"ITEM_PURCHASE": "item", "ABILITY_CAST": "ability",
         "BUYBACK": "buyback"}

# Способности и предметы, которые ядро не смогло разрешить по string
# table, приходят как "#123". Писать их бессмысленно: имя не
# восстановить, а строка займёт место и попадёт в агрегаты.
UNRESOLVED_PREFIX = "#"


def _actor(event: dict, hero_team: dict[str, int]) -> tuple[str, int] | None:
    """(нормализованный герой, сторона) или None, если актор не герой.

    Проверяем оба поля, а не одно: см. докстринг модуля.
    """
    for key in ("attacker", "target"):
        hero = _normalize_hero(str(event.get(key) or ""))
        team = hero_team.get(hero)
        if team:
            return hero, int(team)
    return None


def _item_name(event: dict) -> str:
    """Имя предмета по числовому id из value.

    Неизвестный id сохраняется как `item_<id>`, а не отбрасывается:
    ценность записи — В ТАЙМИНГЕ, а не в названии. Справочник устареет
    с новым патчем, тайминг — нет.
    """
    try:
        item_id = int(event.get("value_amount") or 0)
    except (TypeError, ValueError):
        return ""
    if item_id <= 0:
        return ""
    return _ITEM_IDS.get(item_id, f"item_{item_id}")


def _name_of(event: dict, kind: str, hero: str) -> str:
    """Имя предмета/способности события.

    У ПОКУПКИ в живых данных строкового имени нет вовсе: inflictor пуст,
    target — сам герой-покупатель, а предмет закодирован числом в value.
    Поэтому у kind='item' сначала пробуется справочник id→имя.

    Строковый поиск при этом СОХРАНЁН запасным путём. Спринт 99 полагался
    только на строки и потерял все покупки; заменить его только на числа
    значило бы поставить ту же ставку с другой стороны — а форма события
    зависит от версии ядра-парсера, и обе формы уже встречались.
    """
    if kind == "buyback":
        return "buyback"
    if kind == "item":
        name = _item_name(event)
        if name:
            return name
    for key in ("inflictor", "target"):
        raw = str(event.get(key) or "").strip()
        if not raw or raw.startswith(UNRESOLVED_PREFIX):
            continue
        if _normalize_hero(raw) == hero:
            continue
        return raw
    return ""


def build_timings(events: list[dict], hero_team: dict[str, int]
                  ) -> list[dict]:
    """Строки MatchHeroTimings матча (без match_id — его ставит раннер)."""
    norm_team = {_normalize_hero(h): t for h, t in (hero_team or {}).items()}
    acc: dict[tuple, dict] = {}

    for e in events or []:
        kind = KINDS.get(str(e.get("event_type") or ""))
        if kind is None:
            continue
        who = _actor(e, norm_team)
        if who is None:
            continue
        hero, team = who
        name = _name_of(e, kind, hero)
        if not name:
            continue
        t = int(e.get("game_time") or 0)
        key = (team, hero, kind, name)
        row = acc.get(key)
        if row is None:
            row = acc[key] = {"team": team, "hero": hero, "kind": kind,
                              "name": name, "first_time": t, "last_time": t,
                              "casts_early": 0, "casts_mid": 0,
                              "casts_late": 0}
        # first_time считаем минимумом, а не «первым встреченным»: раннер
        # сортирует события по времени, но полагаться на порядок входа в
        # агрегате нельзя — он тихо развалится, если запрос когда-нибудь
        # потеряет ORDER BY.
        row["first_time"] = min(row["first_time"], t)
        row["last_time"] = max(row["last_time"], t)
        row[f"casts_{phase_of(t)}"] += 1

    return [acc[k] for k in sorted(acc)]
