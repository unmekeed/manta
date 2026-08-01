"""Восстановление драк из комбат-лога и позиций (A14, спринт 84).

Зачем отдельная таблица, а не расчёт на лету. `ReplayEvents` живёт 14
дней (миграция 007) — это единственный источник того, кто кого убил.
Всё, что старше, стирается безвозвратно, и восстановить драки уже
неоткуда: реплеи Valve тоже удаляет. Агрегат драки занимает десятки байт
против сотен килобайт сырых событий, поэтому `MatchFights` хранится без
TTL и копится с сегодняшнего дня — иначе к моменту, когда данных хватит
на модель, истории не будет.

Что такое «драка» здесь: группа смертей героев, идущих подряд не дальше
FIGHT_GAP_S друг от друга. Это операциональное определение, а не
попытка угадать намерения: размен подряд — то, что реально решает
исход, и именно его мы хотим объяснять («полез 3v5 при шансе 18%»).
"""
from __future__ import annotations

import math

from .features import FIGHT_R, _normalize_hero

# Смерти дальше этого промежутка считаются разными стычками. 20 секунд —
# компромисс: время респауна в ранней игре и типичная пауза между
# волнами размена в тимфайте.
FIGHT_GAP_S = 20
# Снапшот позиции считается описывающим момент смерти, если отстоит не
# дальше этого. Парсер сэмплирует позиции чаще, но на границах окон
# бывают пропуски.
POS_TOLERANCE_S = 15


def _positions_by_hero(positions: list[dict]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for p in positions:
        hero = _normalize_hero(str(p.get("hero", "")))
        out.setdefault(hero, []).append(
            (int(p["game_time"]), float(p["x"]), float(p["y"]),
             int(p.get("is_alive", 1))))
    for v in out.values():
        v.sort()
    return out


def _pos_at(samples: list[tuple], t: int) -> tuple[float, float] | None:
    """Позиция героя в момент t по ближайшему снапшоту (в пределах допуска)."""
    best = None
    for gt, x, y, _alive in samples:
        d = abs(gt - t)
        if best is None or d < best[0]:
            best = (d, x, y)
        if gt > t + POS_TOLERANCE_S:
            break
    if best is None or best[0] > POS_TOLERANCE_S:
        return None
    return best[1], best[2]


def _alive_near(samples_by_hero: dict[str, list[tuple]],
                hero_team: dict[str, int], t: int,
                cx: float, cy: float) -> tuple[set[str], set[str]]:
    """Живые герои каждой стороны в радиусе FIGHT_R — ИМЕНАМИ, не числом.

    Множества, а не счётчики, потому что участников придётся объединять
    с погибшими: снапшот на начало драки может ещё помечать будущего
    погибшего живым, и суммирование дало бы его дважды.
    """
    norm_team = {_normalize_hero(h): team for h, team in hero_team.items()}
    n: dict[int, set[str]] = {2: set(), 3: set()}
    for hero, samples in samples_by_hero.items():
        team = norm_team.get(hero)
        if team not in n:
            continue
        # ближайший снапшот и его признак жизни
        best = None
        for gt, x, y, alive in samples:
            d = abs(gt - t)
            if best is None or d < best[0]:
                best = (d, x, y, alive)
            if gt > t + POS_TOLERANCE_S:
                break
        if best is None or best[0] > POS_TOLERANCE_S or not best[3]:
            continue
        if math.hypot(best[1] - cx, best[2] - cy) <= FIGHT_R:
            n[team].add(hero)
    return n[2], n[3]


def detect_fights(kills: list[dict], positions: list[dict],
                  hero_team: dict[str, int]) -> list[dict]:
    """Строки MatchFights из смертей героев.

    kills — события KILL по героям: {"game_time", "target", "attacker"}.
    Возвращает по строке на драку; порядок — хронологический.

    Исход считается по размену смертей, а НЕ по тому, кто остался на
    точке: «выиграл драку» здесь означает «потерял меньше», и это
    единственное, что комбат-лог позволяет утверждать честно.
    """
    norm_team = {_normalize_hero(h): t for h, t in hero_team.items()}
    deaths = []
    for k in kills:
        hero = _normalize_hero(str(k.get("target", "")))
        team = norm_team.get(hero)
        if team in (2, 3):
            deaths.append((int(k["game_time"]), hero, team,
                           _normalize_hero(str(k.get("attacker", "")))))
    if not deaths:
        return []
    deaths.sort()

    by_hero = _positions_by_hero(positions)

    # Кластеризация по времени: разрыв больше FIGHT_GAP_S начинает новую.
    clusters: list[list[tuple]] = [[deaths[0]]]
    for d in deaths[1:]:
        if d[0] - clusters[-1][-1][0] > FIGHT_GAP_S:
            clusters.append([d])
        else:
            clusters[-1].append(d)

    out = []
    for idx, cluster in enumerate(clusters):
        start = cluster[0][0]
        end = cluster[-1][0]
        r_deaths = sum(1 for _t, _h, team, _a in cluster if team == 2)
        d_deaths = sum(1 for _t, _h, team, _a in cluster if team == 3)

        # Центр стычки — средняя позиция погибших. Позиция мёртвого героя
        # берётся на момент смерти, пока труп ещё лежит там, где убили.
        pts = []
        for t, hero, _team, _a in cluster:
            p = _pos_at(by_hero.get(hero, []), t)
            if p:
                pts.append(p)
        if pts:
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
        else:
            cx = cy = float("nan")

        if pts:
            r_near, d_near = _alive_near(by_hero, hero_team, start, cx, cy)
        else:
            r_near, d_near = set(), set()
        # Участники — ОБЪЕДИНЕНИЕ тех, кто был рядом на начало драки, и
        # тех, кто в ней погиб. Именно объединение, а не сумма: снапшот
        # на момент start может ещё помечать будущего погибшего живым, и
        # сложение посчитало бы его дважды.
        r_dead = {h for _t, h, team, _a in cluster if team == 2}
        d_dead = {h for _t, h, team, _a in cluster if team == 3}
        row = {
            "fight_id": idx,
            "start_time": start,
            "end_time": end,
            "radiant_participants": len(r_near | r_dead),
            "dire_participants": len(d_near | d_dead),
            "radiant_deaths": r_deaths,
            "dire_deaths": d_deaths,
            # +1 Radiant разменял выгоднее, −1 Dire, 0 поровну
            "outcome": (d_deaths > r_deaths) - (r_deaths > d_deaths),
        }
        # Координаты пишем ТОЛЬКО когда они известны: json.dumps(nan)
        # выдаёт нестандартный литерал NaN, который ClickHouse в
        # JSONEachRow не примет и уронит вставку всей пачки. Отсутствие
        # ключа даёт DEFAULT nan — тот же смысл, но без риска.
        if pts:
            row["x"] = round(cx, 1)
            row["y"] = round(cy, 1)
        out.append(row)
    return out
