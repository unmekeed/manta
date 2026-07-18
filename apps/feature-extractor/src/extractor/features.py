"""Расчёт фич матча (Гл. 6) из сырых таблиц ClickHouse.

Чистые функции без I/O: вход — списки словарей (строки EconomyTimeline и
KILL-события), выход — строки витрин PlayerMatchFeatures и
MatchTimelineFeatures. Тестируются на синтетике без инфраструктуры.

Конвенции данных:
- player_id 0-4 — Radiant (team 2), 5-9 — Dire (team 3), нумерация слотов
  совпадает с порядком ростера в CDemoFileInfo (см. parser-svc);
- EconomyTimeline сэмплируется каждые ~10 с (300 тиков), значения
  накопительные (total_gold, total_xp, lh, dn);
- game_time в сырых таблицах — секунды реплея (включая пик-фазу), поэтому
  «минута N» отсчитывается от первого сэмпла с ненулевой экономикой.
"""
from __future__ import annotations

from dataclasses import dataclass

FEATURE_VERSION = "1.3.0"  # 1.3.0: + alive_diff, towers_diff, rax_diff

WINDOW_S = 60  # шаг таймлайна фич


@dataclass(frozen=True)
class Roster:
    """Ростер матча: маппинги слот → команда/герой/имя."""

    teams: dict[int, int]        # player_id -> 2|3
    heroes: dict[int, str]       # player_id -> npc_dota_hero_*
    names: dict[int, str]        # player_id -> ник
    hero_team: dict[str, int]    # npc_dota_hero_* -> 2|3
    winner: int                  # 2|3

    @staticmethod
    def from_players(players: list[dict], winner: str) -> "Roster":
        """players — из payload replay.parsed (порядок: Radiant, Dire)."""
        teams: dict[int, int] = {}
        heroes: dict[int, str] = {}
        names: dict[int, str] = {}
        hero_team: dict[str, int] = {}
        radiant_i = 0
        dire_i = 0
        for p in players:
            team = int(p["team"])
            if team == 2:
                pid = radiant_i
                radiant_i += 1
            elif team == 3:
                pid = 5 + dire_i
                dire_i += 1
            else:
                continue
            teams[pid] = team
            heroes[pid] = p.get("hero", "")
            names[pid] = p.get("name", "")
            if p.get("hero"):
                hero_team[p["hero"]] = team
        return Roster(teams=teams, heroes=heroes, names=names,
                      hero_team=hero_team,
                      winner=2 if winner == "Radiant" else 3)


def _game_start(economy: list[dict]) -> int:
    """Секунда первого сэмпла с ненулевой экономикой (конец пик-фазы)."""
    for row in economy:  # ожидается сортировка по game_time
        if row["total_gold"] > 0 or row["total_xp"] > 0:
            return int(row["game_time"])
    return 0


def _value_at(samples: list[tuple[int, dict]], t: int) -> dict | None:
    """Последний сэмпл игрока с game_time <= t (point-in-time, no leakage)."""
    best = None
    for gt, row in samples:
        if gt <= t:
            best = row
        else:
            break
    return best


def player_features(economy: list[dict], roster: Roster,
                    duration_s: float,
                    positions: list[dict] | None = None) -> list[dict]:
    """Строки PlayerMatchFeatures из накопительной экономики.

    positions — снапшоты PositionSnapshots ({"game_time","hero","x","y"},
    имя героя — класс сущности) для определения линии и исхода лейнинга;
    None → lane='' и lane_nw_diff_at_10=0 (старые данные).
    """
    by_player: dict[int, list[tuple[int, dict]]] = {}
    for row in sorted(economy, key=lambda r: (r["player_id"], r["game_time"])):
        by_player.setdefault(int(row["player_id"]), []).append(
            (int(row["game_time"]), row))

    start = _game_start(economy)
    minutes = max((duration_s if duration_s > 0 else 1) / 60.0, 1e-6)

    finals: dict[int, dict] = {
        pid: samples[-1][1] for pid, samples in by_player.items() if samples
    }
    team_networth = {2: 0, 3: 0}
    for pid, row in finals.items():
        team = roster.teams.get(pid, 0)
        if team in team_networth:
            team_networth[team] += int(row["net_worth"])

    # Линии: позиции группируются по нормализованному имени героя и
    # сопоставляются слотам через ростер.
    by_hero: dict[str, list[tuple[int, float, float]]] = {}
    for p in positions or []:
        by_hero.setdefault(_normalize_hero(str(p.get("hero", ""))), []).append(
            (int(p["game_time"]), float(p["x"]), float(p["y"])))
    hero_lanes = detect_lanes(by_hero, start) if by_hero else {}
    player_lane = {
        pid: hero_lanes.get(_normalize_hero(hero), "")
        for pid, hero in roster.heroes.items()
    }

    nw10 = {pid: int((_value_at(samples, start + 600) or {}).get("net_worth", 0))
            for pid, samples in by_player.items()}

    def lane_diff(pid: int) -> int:
        """nw@10 против среднего прямых оппонентов по линии (0 — нет данных)."""
        lane = player_lane.get(pid, "")
        if lane in ("", "roam"):
            return 0
        my_team = roster.teams.get(pid, 0)
        opp = [nw10[q] for q in nw10
               if roster.teams.get(q, 0) not in (0, my_team)
               and player_lane.get(q, "") == lane]
        if not opp:
            return 0
        return nw10[pid] - round(sum(opp) / len(opp))

    out = []
    for pid, samples in sorted(by_player.items()):
        if not samples:
            continue
        team = roster.teams.get(pid, 0)
        final = finals[pid]
        at5 = _value_at(samples, start + 5 * 60) or {}
        at10 = _value_at(samples, start + 10 * 60) or {}
        at20 = _value_at(samples, start + 20 * 60) or {}
        tn = team_networth.get(team, 0)
        out.append({
            "player_id": pid,
            "team": team,
            "hero": roster.heroes.get(pid, ""),
            "player_name": roster.names.get(pid, ""),
            "won": 1 if team == roster.winner else 0,
            "duration_s": int(duration_s),
            "gpm": round(int(final["total_gold"]) / minutes, 2),
            "xpm": round(int(final["total_xp"]) / minutes, 2),
            "lh_at_5": int(at5.get("lh", 0)),
            "dn_at_5": int(at5.get("dn", 0)),
            "lh_at_10": int(at10.get("lh", 0)),
            "dn_at_10": int(at10.get("dn", 0)),
            "lane": player_lane.get(pid, ""),
            "lane_nw_diff_at_10": lane_diff(pid),
            "net_worth_at_10": int(at10.get("net_worth", 0)),
            "net_worth_at_20": int(at20.get("net_worth", 0)),
            "net_worth_final": int(final["net_worth"]),
            "gold_share": round(int(final["net_worth"]) / tn, 4) if tn else 0.0,
            "feature_version": FEATURE_VERSION,
        })
    return out


# Полудиагональ карты: координаты героев лежат в ~[-8700, 8900].
MAP_HALF_DIAG = 8000.0


def _normalize_hero(name: str) -> str:
    """Ключ сопоставления имён героев между источниками: класс сущности
    (CDOTA_Unit_Hero_DoomBringer) и npc-имя (npc_dota_hero_doom_bringer)
    совпадают после отбрасывания префикса, подчёркиваний и регистра."""
    for prefix in ("CDOTA_Unit_Hero_", "npc_dota_hero_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.replace("_", "").lower()


# Пороги классификации линии по средней проекции d = x - y за 2-8 минуты:
# top d <= -4500, bot d >= 4500, mid |d| < 3000, иначе jungle/roam.
# Калибровано по реальным матчам (лейнеры |d| ~ 6800-10000, миды ~ 350).
LANE_EDGE_D = 4500.0
LANE_MID_D = 3000.0
LANING_FROM_S = 120
LANING_TO_S = 480


def detect_lanes(positions_by_hero: dict[str, list[tuple[int, float, float]]],
                 game_start: int) -> dict[str, str]:
    """Линия каждого героя по средним позициям фазы лейнинга.

    positions_by_hero: НОРМАЛИЗОВАННОЕ имя героя (см. _normalize_hero) →
    [(game_time, x, y)]. Возвращает имя → top|mid|bot|roam.
    """
    lo = game_start + LANING_FROM_S
    hi = game_start + LANING_TO_S
    lanes: dict[str, str] = {}
    for hero, pts in positions_by_hero.items():
        ds = [x - y for t, x, y in pts if lo <= t <= hi]
        if not ds:
            lanes[hero] = "roam"
            continue
        d = sum(ds) / len(ds)
        if abs(d) < LANE_MID_D:
            lanes[hero] = "mid"
        elif d <= -LANE_EDGE_D:
            lanes[hero] = "top"
        elif d >= LANE_EDGE_D:
            lanes[hero] = "bot"
        else:
            lanes[hero] = "roam"
    return lanes


def position_advance_by_window(positions: list[dict], max_t: int) -> dict[int, float]:
    """Территориальное продвижение по минутным окнам.

    positions — снапшоты {"game_time", "x", "y"} (все герои). Проекция
    на диагональ «фонтан Radiant (−,−) → фонтан Dire (+,+)»:
    (x + y) / (2·HALF_DIAG), клип в [-1, 1]. Значение окна — среднее по
    всем снапшотам окна: бой у базы Dire → +, у базы Radiant → −.
    Прокси Map Control (Гл. 6.1.3); полноценный контроль по обзору
    требует вардов — позже.
    """
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    for p in positions:
        t = int(p["game_time"])
        if t > max_t:
            continue
        w = ((t + WINDOW_S - 1) // WINDOW_S) * WINDOW_S  # окно, куда попадает t
        proj = (float(p["x"]) + float(p["y"])) / (2.0 * MAP_HALF_DIAG)
        proj = min(max(proj, -1.0), 1.0)
        sums[w] = sums.get(w, 0.0) + proj
        counts[w] = counts.get(w, 0) + 1
    out = {w: sums[w] / counts[w] for w in sums}
    # Окна без снапшотов наследуют последнее известное значение.
    last = 0.0
    for w in range(WINDOW_S, max_t + 1, WINDOW_S):
        if w in out:
            last = out[w]
        else:
            out[w] = last
    return out


def building_diffs_by_window(building_kills: list[dict], max_t: int
                             ) -> dict[int, tuple[int, int]]:
    """Накопительные (towers_diff, rax_diff) по минутным окнам.

    building_kills — события KILL со зданиями-жертвами
    ({"game_time", "target": npc_dota_goodguys_tower1_mid | *_rax_* …}).
    Снесённое здание goodguys (Radiant) — очко Dire, badguys — очко Radiant:
    diff = снесено Radiant'ом − снесено Dire'ом (знак согласован с
    networth_diff: + в пользу Radiant).
    """
    events = []  # (t, is_tower, delta)
    for k in building_kills:
        target = str(k["target"])
        if "tower" in target:
            kind = "tower"
        elif "_rax_" in target:
            kind = "rax"
        else:
            continue
        delta = 1 if "badguys" in target else -1
        events.append((int(k["game_time"]), kind, delta))
    events.sort()
    out: dict[int, tuple[int, int]] = {}
    towers = rax = 0
    i = 0
    for w in range(WINDOW_S, max_t + 1, WINDOW_S):
        while i < len(events) and events[i][0] <= w:
            _, kind, delta = events[i]
            if kind == "tower":
                towers += delta
            else:
                rax += delta
            i += 1
        out[w] = (towers, rax)
    return out


def alive_diff_by_window(positions: list[dict], hero_team: dict[str, int],
                         max_t: int) -> dict[int, float]:
    """Живые герои Radiant − Dire по минутным окнам.

    positions — снапшоты PositionSnapshots ({"game_time", "hero",
    "is_alive"}). Значение окна — по ПОСЛЕДНЕМУ снапшоту каждого героя в
    окне; герой без снапшота в окне считается живым (мёртвых парсер
    продолжает сэмплировать, пропуски — артефакт начала матча).
    """
    norm_team = {_normalize_hero(h): t for h, t in hero_team.items()}
    # окно → герой → is_alive последнего снапшота
    last_in_window: dict[int, dict[str, int]] = {}
    for p in positions:
        t = int(p["game_time"])
        if t > max_t or "is_alive" not in p:
            continue
        w = ((t + WINDOW_S - 1) // WINDOW_S) * WINDOW_S
        last_in_window.setdefault(w, {})[_normalize_hero(str(p["hero"]))] = \
            int(p["is_alive"])
    out: dict[int, float] = {}
    for w in range(WINDOW_S, max_t + 1, WINDOW_S):
        snap = last_in_window.get(w, {})
        alive = {2: 0, 3: 0}
        for hero, team in norm_team.items():
            if team not in alive:
                continue
            alive[team] += snap.get(hero, 1)  # нет снапшота → жив
        out[w] = float(alive[2] - alive[3])
    return out


def timeline_features(economy: list[dict], kills: list[dict],
                      roster: Roster,
                      positions: list[dict] | None = None,
                      building_kills: list[dict] | None = None) -> list[dict]:
    """Строки MatchTimelineFeatures: поминутные командные дифференциалы.

    kills — события KILL по героям: {"game_time": int, "target": npc_dota_hero_*}.
    kills_radiant — убийства, СОВЕРШЁННЫЕ Radiant (жертва из Dire), накопительно.
    positions — снапшоты позиций героев (PositionSnapshots) для
    position_advance и alive_diff; None/пусто → фича 0 (старые данные).
    building_kills — события KILL со зданиями для towers_diff/rax_diff.
    """
    by_player: dict[int, list[tuple[int, dict]]] = {}
    max_t = 0
    for row in sorted(economy, key=lambda r: (r["player_id"], r["game_time"])):
        gt = int(row["game_time"])
        max_t = max(max_t, gt)
        by_player.setdefault(int(row["player_id"]), []).append((gt, row))

    kill_times = sorted(
        (int(k["game_time"]), roster.hero_team.get(k["target"], 0))
        for k in kills
    )

    advance = position_advance_by_window(positions or [], max_t)
    buildings = building_diffs_by_window(building_kills or [], max_t)
    alive = alive_diff_by_window(positions or [], roster.hero_team, max_t)

    out = []
    radiant_win = 1 if roster.winner == 2 else 0
    for t in range(WINDOW_S, max_t + 1, WINDOW_S):
        nw = {2: 0, 3: 0}
        xp = {2: 0, 3: 0}
        for pid, samples in by_player.items():
            team = roster.teams.get(pid, 0)
            if team not in nw:
                continue
            row = _value_at(samples, t)
            if row:
                nw[team] += int(row["net_worth"])
                xp[team] += int(row["total_xp"])
        kills_r = sum(1 for kt, victim_team in kill_times
                      if kt <= t and victim_team == 3)
        kills_d = sum(1 for kt, victim_team in kill_times
                      if kt <= t and victim_team == 2)
        towers, rax = buildings.get(t, (0, 0))
        out.append({
            "game_time": t,
            "networth_diff": nw[2] - nw[3],
            "xp_diff": xp[2] - xp[3],
            "kills_radiant": kills_r,
            "kills_dire": kills_d,
            "position_advance": round(advance.get(t, 0.0), 4),
            "alive_diff": alive.get(t, 0.0),
            "towers_diff": float(towers),
            "rax_diff": float(rax),
            "radiant_win": radiant_win,
            "feature_version": FEATURE_VERSION,
        })
    return out
