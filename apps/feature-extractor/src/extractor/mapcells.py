"""Тепловые карты матча по фазам игры (спринт 98).

Что считается и откуда:

  presence  где сторона находилась          PositionSnapshots
  farm      где ФАРМИЛА                      PositionSnapshots + EconomyTimeline
  farm_core то же, но ТОЛЬКО позиции 1–3     PositionSnapshots + EconomyTimeline
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

import bisect
import math

from dota_map import in_bounds, unit_to_grid, world_to_unit

from .features import _normalize_hero

# Сторона квадратной сетки. 32 даёт клетку примерно 500 игровых единиц —
# половина радиуса обзора варда. Мельче: карта становится шумной на
# десятках матчей и тяжелеет квадратично. Крупнее: лес перестаёт
# отличаться от линии, а именно это различие карта и должна показывать.
GRID = 32
# Границы карты и переводы координат живут в libs/dota_map.py — одном
# месте на весь проект.
#
# Раньше здесь стояло MAP_HALF = 8000.0 с комментарием «совпадает с
# MAP_HALF_DIAG, по которому нормируются позиционные фичи». Это была
# ошибка: MAP_HALF_DIAG — половина ДИАГОНАЛИ, а тут нужна половина
# СТОРОНЫ, и отличаются они в √2 раз. Пока тепловая карта рисовалась
# своей самодельной схемой, расхождение было незаметно; под общей
# подложкой оно разъедет тепловые пятна и варды.

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
    if not in_bounds(fx, fy):
        return None
    return unit_to_grid(*world_to_unit(fx, fy), GRID)


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


# Сколько игроков в команде считаем корами. Три: позиции 1, 2 и 3.
CORES_PER_TEAM = 3


def core_heroes(economy: list[dict], teams: dict[int, int],
                heroes: dict[int, str]) -> set[str]:
    """Герои позиций 1–3 обеих сторон — по добиткам к концу матча.

    ПОЧЕМУ ДОБИТКИ, А НЕ NET WORTH. И то и другое ранжирует команду, но
    отвечают они на разные вопросы. Net worth растёт от убийств, и
    накормленный пос-4 обгоняет по нему честно фармящий оффлейн — а карта
    строится про ФАРМ, и фармит тут именно оффлейнер. Добитки же почти
    невозможно набрать, не стоя на линии и в лесу, то есть ровно там, где
    карта фарма и должна быть острой.

    ЗАЧЕМ ВООБЩЕ РАНГ, А НЕ РОЛЬ ИЗ ИСТОЧНИКА. Роли (lane_role) есть
    только у JSON-матчей OpenDota, а карты строятся ТОЛЬКО по реплеям —
    брать роль оттуда значило бы иметь её у части матчей и не иметь у
    остальных. Ранг по добиткам считается из того же реплея, что и всё
    остальное на этой карте.

    ЧЕГО НЕ ДЕЛАЕТ: не отличает позицию 1 от 2 и 3. Карте это и не нужно,
    ей нужна граница «кор / саппорт», а она проходит именно после
    третьего места.
    """
    # Последний по времени сэмпл каждого игрока: EconomyTimeline
    # отсортирован, но полагаться на порядок нельзя — строки приходят и
    # из бэкфилла, где сортировка своя.
    last: dict[int, tuple[int, int]] = {}
    for row in economy or []:
        try:
            pid = int(row["player_id"])
            t = int(row.get("game_time") or 0)
            lh = int(row.get("lh") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if pid not in last or t >= last[pid][0]:
            last[pid] = (t, lh)

    out: set[str] = set()
    for team in (2, 3):
        mates = [pid for pid, side in (teams or {}).items() if side == team]
        # Если про добитки команды НИЧЕГО не известно, ранжировать нечего:
        # у всех ноль, и сортировка молча выберет первых троих по номеру
        # слота. Это выглядело бы как честный отбор коров и было бы
        # случайным — а по такой карте потом читают, где фармит керри.
        # Пусто означает «неизвестно», и вид просто не пишется.
        if not any(last.get(pid, (0, 0))[1] > 0 for pid in mates):
            continue
        ranked = sorted(
            mates,
            # Добитки по убыванию; player_id вторым ключом — не для
            # справедливости, а для устойчивости: при равных добитках
            # (бывает в коротких матчах) произвольный порядок давал бы
            # РАЗНЫЕ карты при переразборе одного и того же матча.
            key=lambda pid: (-last.get(pid, (0, 0))[1], pid))
        for pid in ranked[:CORES_PER_TEAM]:
            hero = (heroes or {}).get(pid) or ""
            if hero:
                out.add(_normalize_hero(hero))
    return out


class FarmClock:
    """Когда каждый герой ФАКТИЧЕСКИ фармил — по росту добиток.

    ЗАЧЕМ ОТДЕЛЬНАЯ СУЩНОСТЬ (спринт 141). До неё «фарм» определялся как
    «герой жив, и рядом нет врага». Это не измерение фарма, а подмена его
    признаком безопасности, и подмена дырявая: на ФОНТАНЕ оба условия
    выполняются идеально. Герой воскрес, врагов рядом нет — и каждые
    десять секунд ожидания телепорта капали в карту фарма. То же самое
    происходило до гудка и при закупке, и на карте вырастали яркие пятна
    ровно там, где не фармят никогда.

    Заметить это можно было только глазом и только после того, как под
    карту легла настоящая подложка: на стилизованной схеме угол карты
    ничем не отличался от леса.

    ЧЕМ МЕРЯЕМ. Позиции и экономика пишутся парсером в ОДНОМ цикле, раз в
    300 тиков, и game_time у них поэтому совпадает тик в тик. Значит для
    каждого интервала между сэмплами (около десяти секунд) известно, вырос
    ли у героя счётчик добиток. Вырос — герой в этот интервал фармил, где
    бы он ни стоял; не вырос — не фармил, даже если стоял в лесу и ему
    ничего не угрожало.

    Добитки считаются и по крипам линии, и по нейтралам, поэтому лесной
    маршрут виден так же, как линейный.

    ЧЕГО НЕ ДЕЛАЕТ. Не отличает добивание от подбора рун и не делит
    интервал: если герой полсэмпла шёл, а полсэмпла бил крипа, весь
    интервал считается фармом. Точнее сетка сэмплов и не позволяет, а
    десять секунд — это меньше клетки карты по расстоянию.
    """

    def __init__(self, economy: list[dict], heroes: dict[int, str]):
        by_pid: dict[int, list[tuple[int, int]]] = {}
        for row in economy or []:
            try:
                pid = int(row["player_id"])
                t = int(row.get("game_time") or 0)
                lh = int(row.get("lh") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            by_pid.setdefault(pid, []).append((t, lh))

        # hero -> (времена сэмплов, рос ли счётчик в интервале ПОСЛЕ них)
        self._by_hero: dict[str, tuple[list[int], list[bool]]] = {}
        for pid, samples in by_pid.items():
            hero = (heroes or {}).get(pid) or ""
            if not hero:
                continue
            # Сортировка ОБЯЗАТЕЛЬНА: запрос экономики идёт без ORDER BY,
            # а ClickHouse не обещает порядок. Несортированные сэмплы дают
            # случайные «падения» счётчика между соседями, и рост
            # обнаруживается там, где его не было.
            samples.sort()
            times = [t for t, _ in samples]
            grew = [samples[i + 1][1] > samples[i][1]
                    for i in range(len(samples) - 1)]
            self._by_hero[_normalize_hero(hero)] = (times, grew)

    def farming(self, hero: str, t: int) -> bool:
        entry = self._by_hero.get(hero)
        if entry is None:
            return False
        times, grew = entry
        # Интервал, в который попадает позиция: последний сэмпл не позже t.
        i = bisect.bisect_right(times, t) - 1
        return 0 <= i < len(grew) and grew[i]


def _presence_and_farm(acc: dict, positions: list[dict],
                       hero_team: dict[str, int],
                       cores: set[str] | None = None,
                       clock: "FarmClock" = None) -> None:
    """Присутствие и фарм.

    ПРИСУТСТВИЕ — где живой герой находился. Простой факт, считается как
    считался.

    ФАРМ — где герой ФАРМИЛ, то есть где он стоял в те интервалы, когда у
    него рос счётчик добиток (см. FarmClock).

    До спринта 141 фарм определялся иначе: «жив и рядом нет врага».
    Признак безопасности выдавался за признак фарма, и на фонтане он
    срабатывал безотказно — герой воскрес, врагов нет, интервал засчитан.
    Условие про врага теперь убрано СОВСЕМ, а не добавлено к новому:
    держать оба значило бы выбросить фарм на оспариваемой линии, а он
    такой же фарм, как лесной, и для маршрутов нужен не меньше.

    cores — герои позиций 1–3; для них дополнительно копится 'farm_core'.
    Пустое множество означает «кого считать кором, неизвестно», и тогда
    вид не пишется ВОВСЕ: карта фарма коров, посчитанная по всем пятерым,
    была бы неотличима от честной и врала бы молча.
    """
    cores = cores or set()
    clock = clock or FarmClock([], {})
    for p in positions:
        t = int(p.get("game_time") or 0)
        hero = _normalize_hero(str(p.get("hero", "")))
        team = hero_team.get(hero)
        if not team or not int(p.get("is_alive") or 0):
            continue
        c = cell(p.get("x"), p.get("y"))
        if c is None:
            continue
        phase = phase_of(t)
        key = (phase, team, "presence", c[0], c[1])
        acc[key] = acc.get(key, 0) + 1

        # Нет экономики — нет и фарма: пустой FarmClock отвечает «нет» на
        # любой вопрос. Отдельной проверки «данные вообще есть» тут была
        # заведена и убрана: она ничего не меняла, потому что пустой
        # словарь и так не даёт ни одного положительного ответа.
        if not clock.farming(hero, t):
            continue
        key = (phase, team, "farm", c[0], c[1])
        acc[key] = acc.get(key, 0) + 1
        if hero in cores:
            key = (phase, team, "farm_core", c[0], c[1])
            acc[key] = acc.get(key, 0) + 1


def build_cells(positions: list[dict], map_events: list[dict],
                fights: list[dict], hero_team: dict[str, int],
                cores: set[str] | None = None,
                economy: list[dict] | None = None,
                heroes: dict[int, str] | None = None,
                ) -> list[dict]:
    """Строки MatchMapCells матча (без match_id — его ставит раннер).

    map_events: строки ReplayEvents с game_time, event_type, x, y и
    именем героя (attacker/target) — по нему определяется сторона.
    fights: строки MatchFights (уже посчитанные драки с координатами).
    """
    acc: dict[tuple, int] = {}
    hero_team = _norm_team(hero_team or {})
    _presence_and_farm(acc, positions or [], hero_team,
                       {_normalize_hero(h) for h in (cores or set())},
                       FarmClock(economy or [], heroes or {}))

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
