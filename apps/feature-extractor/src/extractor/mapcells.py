"""Тепловые карты матча по фазам игры (спринт 98).

Что считается и откуда:

  presence  где сторона находилась          PositionSnapshots
  farm      где фармила БЕЗОПАСНО            PositionSnapshots
  death     где погибали герои               ReplayEvents KILL
  ward      где ставили обзор                ReplayEvents WARD_PLACE
  smoke     откуда начинали смоук-ганк       ReplayEvents SMOKE
  fight     где происходили стычки           MatchFights

Всё — из реплея, и это осознанное ограничение: присутствие и маршруты
фарма существуют только в позициях, а позиции есть только там. JSON-путь
даёт координаты вардов, но подмешивать их значило бы построить карту, у
которой один слой по одной популяции матчей, а другой по другой.

Зачем агрегат, а не запрос по сырью — см. миграцию 020: ReplayEvents
живёт 14 дней, а карта нужна и через год.

Функции чистые (списки словарей → списки словарей), поэтому проверяются
без ClickHouse.
"""
from __future__ import annotations

import math

from .features import FIGHT_R, _normalize_hero

# Сторона квадратной сетки. 32 даёт клетку примерно 500 игровых единиц —
# половина радиуса обзора варда. Мельче: карта становится шумной на
# десятках матчей и тяжелеет квадратично. Крупнее: лес перестаёт
# отличаться от линии, а именно это различие карта и должна показывать.
GRID = 32
# Половина стороны игровой карты в юнитах (совпадает с MAP_HALF_DIAG,
# по которому нормируются позиционные фичи).
MAP_HALF = 8000.0

# Границы фаз в игровых секундах. По времени, а не по доле матча:
# 10-я минута значит одно и то же в игре на 25 и на 60 минут.
EARLY_END = 600      # лайнинг
MID_END = 1500       # середина; дальше — поздняя игра


def phase_of(game_time) -> str:
    t = int(game_time or 0)
    if t < EARLY_END:
        return "early"
    return "mid" if t < MID_END else "late"


def cell(x, y) -> tuple[int, int] | None:
    """Игровые координаты → клетка сетки; None, если точка вне карты.

    Отбрасывать невалидные точки важнее, чем кажется: до спринта 97
    координаты событий были нулями, и без этой проверки вся карта
    смертей собралась бы в одну центральную клетку, выглядя при этом
    правдоподобно.
    """
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if math.isnan(fx) or math.isnan(fy):
        return None
    if abs(fx) > MAP_HALF or abs(fy) > MAP_HALF:
        return None
    gx = int((fx + MAP_HALF) / (2 * MAP_HALF) * GRID)
    gy = int((fy + MAP_HALF) / (2 * MAP_HALF) * GRID)
    return min(gx, GRID - 1), min(gy, GRID - 1)


def _add(acc: dict, phase: str, team: int, kind: str, pos) -> None:
    c = cell(*pos) if pos is not None else None
    if c is None or not team:
        return
    acc[(phase, int(team), kind, c[0], c[1])] = \
        acc.get((phase, int(team), kind, c[0], c[1]), 0) + 1


def _norm_team(hero_team: dict[str, int]) -> dict[str, int]:
    """Ключи ростера — СЫРЫЕ имена (npc_dota_hero_axe), а в снапшотах и
    событиях они приходят в разных написаниях. Нормализуем словарь один
    раз: искать по ненормализованному ключу — значит не найти никого и
    получить пустую карту, которая выглядит как «матч без событий»."""
    return {_normalize_hero(h): t for h, t in hero_team.items()}


def _presence_and_farm(acc: dict, positions: list[dict],
                       hero_team: dict[str, int]) -> None:
    """Присутствие и безопасный фарм.

    «Фарм» — не отдельный источник данных, а присутствие ЖИВОГО героя
    там, где рядом нет врага. Именно это отличает фарм-маршрут от
    маршрута к драке, и именно это различие видно на карте: у команды,
    зажатой на своей половине, безопасная зона схлопывается.
    """
    by_time: dict[int, list[dict]] = {}
    for p in positions:
        by_time.setdefault(int(p.get("game_time") or 0), []).append(p)

    for t, snap in by_time.items():
        phase = phase_of(t)
        alive = []
        for p in snap:
            team = hero_team.get(_normalize_hero(str(p.get("hero", ""))))
            if not team or not int(p.get("is_alive") or 0):
                continue
            c = cell(p.get("x"), p.get("y"))
            if c is None:
                continue
            alive.append((team, float(p["x"]), float(p["y"]), c))
        for team, x, y, c in alive:
            key = (phase, team, "presence", c[0], c[1])
            acc[key] = acc.get(key, 0) + 1
            near_enemy = any(
                other_team != team
                and math.dist((x, y), (ox, oy)) <= FIGHT_R
                for other_team, ox, oy, _ in alive)
            if not near_enemy:
                key = (phase, team, "farm", c[0], c[1])
                acc[key] = acc.get(key, 0) + 1


def build_cells(positions: list[dict], map_events: list[dict],
                fights: list[dict], hero_team: dict[str, int]
                ) -> list[dict]:
    """Строки MatchMapCells матча (без match_id — его ставит раннер).

    map_events: строки ReplayEvents с game_time, event_type, x, y и
    именем героя (attacker/target) — по нему определяется сторона.
    fights: строки MatchFights (уже посчитанные драки с координатами).
    """
    acc: dict[tuple, int] = {}
    hero_team = _norm_team(hero_team or {})
    _presence_and_farm(acc, positions or [], hero_team)

    for e in map_events or []:
        kind = {"KILL": "death", "WARD_PLACE": "ward",
                "SMOKE": "smoke"}.get(str(e.get("event_type") or ""))
        if kind is None:
            continue
        # У смерти сторона — ПОГИБШЕГО (target), у варда и смоука —
        # поставившего (attacker). Перепутать значило бы получить карту,
        # зеркальную по сторонам, и заметить это было бы почти нечем.
        who = e.get("target") if kind == "death" else e.get("attacker")
        team = hero_team.get(_normalize_hero(str(who or "")))
        _add(acc, phase_of(e.get("game_time")), team, kind,
             (e.get("x"), e.get("y")))

    for f in fights or []:
        phase = phase_of(f.get("start_time"))
        # Драка принадлежит обеим сторонам: карта отвечает на вопрос «где
        # дрались», а не «кто победил» — исход лежит в MatchFights.
        for team in (2, 3):
            _add(acc, phase, team, "fight", (f.get("x"), f.get("y")))

    return [{"phase": phase, "team": team, "kind": kind,
             "gx": gx, "gy": gy, "n": n}
            for (phase, team, kind, gx, gy), n in sorted(acc.items())]
