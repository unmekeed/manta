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

FEATURE_VERSION = "1.0.0"

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
                    duration_s: float) -> list[dict]:
    """Строки PlayerMatchFeatures из накопительной экономики."""
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
            "net_worth_at_10": int(at10.get("net_worth", 0)),
            "net_worth_at_20": int(at20.get("net_worth", 0)),
            "net_worth_final": int(final["net_worth"]),
            "gold_share": round(int(final["net_worth"]) / tn, 4) if tn else 0.0,
            "feature_version": FEATURE_VERSION,
        })
    return out


def timeline_features(economy: list[dict], kills: list[dict],
                      roster: Roster) -> list[dict]:
    """Строки MatchTimelineFeatures: поминутные командные дифференциалы.

    kills — события KILL по героям: {"game_time": int, "target": npc_dota_hero_*}.
    kills_radiant — убийства, СОВЕРШЁННЫЕ Radiant (жертва из Dire), накопительно.
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
        out.append({
            "game_time": t,
            "networth_diff": nw[2] - nw[3],
            "xp_diff": xp[2] - xp[3],
            "kills_radiant": kills_r,
            "kills_dire": kills_d,
            "radiant_win": radiant_win,
            "feature_version": FEATURE_VERSION,
        })
    return out
