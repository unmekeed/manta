"""Сборка отчёта по матчу — чистые функции (контракты Гл. 7: Timeline,
MatchAnalysis).

Вход — строки витрин ClickHouse и WP-кривая от ML Service; выход —
готовые JSON-объекты для MatchReports. Поля laning_score/impact_score —
детерминированные бейзлайн-прокси (честные модели Laning Evaluator и
Impact придут в следующих спринтах, Гл. 6.2.1); отчёт помечен
partial=true, пока оценки — прокси. Нарратив: LLM (narrative.py) с
деградацией на шаблон build_narrative().
"""
from __future__ import annotations

import json
from pathlib import Path

REPORT_VERSION = "0.3.0"  # 0.3.0: LLM-нарратив + narrative_source/gpm

# Словарь героев (libs/data/heroes.json, снапшот OpenDota constants):
# npc_dota_hero_* → числовой id и локализованное имя. Путь зависит от
# раскладки (монорепо: <root>/libs/data; docker-образ: /app/libs/data),
# поэтому кандидаты перебираются; HEROES_PATH переопределяет.


def _load_heroes() -> dict:
    import os
    here = Path(__file__).resolve()
    candidates = [Path(os.environ["HEROES_PATH"])] if os.getenv("HEROES_PATH") else []
    candidates += [
        here.parents[4] / "libs" / "data" / "heroes.json",  # монорепо
        here.parents[2] / "libs" / "data" / "heroes.json",  # /app в образе
    ]
    for c in candidates:
        try:
            return json.loads(c.read_text())
        except (OSError, IndexError):
            continue
    return {}


HEROES = _load_heroes()


def hero_id(npc_name: str) -> int:
    """Числовой ID героя; 0 — неизвестный (новый герой до обновления словаря)."""
    return int(HEROES.get(npc_name, {}).get("id", 0))


def build_timeline(match_id: int, rows: list[dict],
                   wp: list[float]) -> dict:
    """Схема Timeline: точки {game_time, radiant_wp, net_worth_diff}."""
    points = [
        {
            "game_time": int(r["game_time"]),
            "radiant_wp": round(float(p), 4),
            "net_worth_diff": int(r["networth_diff"]),
        }
        for r, p in zip(rows, wp)
    ]
    return {"match_id": match_id, "points": points}


import math


def _laning_score(p: dict) -> float:
    """Лейнинг = исход дуэли на линии: sigmoid от разницы net worth на
    10-й минуте против прямых оппонентов по линии (lane_nw_diff_at_10 из
    витрины; масштаб 1500 золота ≈ выигранная линия). 0.5 — ровная линия.
    Для roam/неопределённой линии диффа нет — fallback на LH-прокси."""
    diff = float(p.get("lane_nw_diff_at_10", 0) or 0)
    if diff != 0:
        return round(1.0 / (1.0 + math.exp(-diff / 1500.0)), 3)
    raw = (float(p.get("lh_at_10", 0)) + 2.0 * float(p.get("dn_at_10", 0))) / 80.0
    return round(min(max(raw, 0.0), 1.0), 3)


def _impact_score(delta_wp_sum: float) -> float:
    """Impact Score (Гл. 6.1.3): Σ ΔWP игрока за матч, сжатая в 0..1
    сигмоидой (масштаб 0.2: суммарные ±20% WP → 0.73/0.27). Игрок без
    атрибутированных событий — нейтральные 0.5."""
    return round(1.0 / (1.0 + math.exp(-delta_wp_sum / 0.2)), 3)


def _median3(values: list[float]) -> list[float]:
    """Медианный фильтр окна 3: гасит одноточечные выбросы калибровки
    (плато изотоники на малых данных), сохраняя устойчивые сдвиги."""
    if len(values) < 3:
        return list(values)
    out = [values[0]]
    for a, b, c in zip(values, values[1:], values[2:]):
        out.append(sorted((a, b, c))[1])
    out.append(values[-1])
    return out


# Порог значимого падения WP команды за минутное окно (Гл. 6.1.1:
# |ΔWP| > τ — критический момент). Смерть героя в таком окне — ошибка.
CRITICAL_DEATH_TAU = 0.06

# -- Safety Index (Гл. 6.1.2) -------------------------------------------------
# Спека определяет SI через распределение ВЕРОЯТНЫХ позиций врагов (live,
# туман войны). Пост-анализ реплея видит ИСТИННЫЕ позиции, поэтому бейзлайн
# считает фактический риск точки в момент смерти: давление живых врагов в
# радиусе + глубина захода на чужую половину. Вероятностная модель по
# последней видимости потребует vision-события (варды) — позже.

SI_PRESSURE_RADIUS = 4000.0   # дальность влияния врага (юниты карты)
SI_PRESSURE_SATURATION = 2.5  # столько «полных» врагов дают pressure = 1
SI_STALE_S = 45               # снапшот позиций старше — не используется
MAP_HALF_DIAG = 8000.0


def _normalize_hero(name: str) -> str:
    """Класс сущности (CDOTA_Unit_Hero_DoomBringer) и npc-имя
    (npc_dota_hero_doom_bringer) совпадают после снятия префикса,
    подчёркиваний и регистра (та же логика, что в feature-extractor)."""
    for prefix in ("CDOTA_Unit_Hero_", "npc_dota_hero_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.replace("_", "").lower()


def index_positions(positions: list[dict]) -> dict[str, list[tuple[int, float, float]]]:
    """PositionSnapshots → нормализованный герой → [(t, x, y)] (сорт. по t)."""
    by_hero: dict[str, list[tuple[int, float, float]]] = {}
    for p in positions:
        by_hero.setdefault(_normalize_hero(str(p.get("hero", ""))), []).append(
            (int(p["game_time"]), float(p["x"]), float(p["y"])))
    for pts in by_hero.values():
        pts.sort()
    return by_hero


# -- Heatmap позиций (Гл. 7, спринт 28) --------------------------------------
# Сетка GRID x GRID поверх мировых координат [-MAP_BOUND, MAP_BOUND];
# ячейка (0,0) — юго-западный угол (база Radiant), ось y вверх. Фронтенд
# рисует плотность на канве миникарты; разреженное представление
# [[gx, gy, count], ...] держит JSON компактным (снапшоты ~1 Гц).

HEATMAP_GRID = 64
MAP_BOUND = 8500.0


def build_heatmap(positions: list[dict], players: list[dict]) -> dict:
    """PositionSnapshots + PlayerMatchFeatures → сетки плотности по игрокам."""
    hero_player = {_normalize_hero(str(p.get("hero", ""))): int(p["player_id"])
                   for p in players if p.get("hero")}
    meta = {int(p["player_id"]): p for p in players}
    cells: dict[int, dict[tuple[int, int], int]] = {}
    scale = HEATMAP_GRID / (2.0 * MAP_BOUND)
    for pos in positions:
        pid = hero_player.get(_normalize_hero(str(pos.get("hero", ""))))
        if pid is None:
            continue
        gx = int((float(pos["x"]) + MAP_BOUND) * scale)
        gy = int((float(pos["y"]) + MAP_BOUND) * scale)
        gx = min(max(gx, 0), HEATMAP_GRID - 1)
        gy = min(max(gy, 0), HEATMAP_GRID - 1)
        grid = cells.setdefault(pid, {})
        grid[(gx, gy)] = grid.get((gx, gy), 0) + 1
    return {
        "grid": HEATMAP_GRID,
        "players": [
            {
                "player_id": pid,
                "hero": str(meta.get(pid, {}).get("hero", "")),
                "player_name": str(meta.get(pid, {}).get("player_name", "")),
                "team": int(meta.get(pid, {}).get("team", 0)),
                "cells": [[gx, gy, n]
                          for (gx, gy), n in sorted(cells[pid].items())],
            }
            for pid in sorted(cells)
        ],
    }


def _pos_at(pts: list[tuple[int, float, float]], t: int,
            near: tuple[float, float] | None = None) -> tuple[float, float] | None:
    """Позиция героя на момент t: последний снапшот <= t (не старше
    SI_STALE_S). Несколько сущностей героя в один момент (иллюзии) —
    берётся ближайшая к точке near (наихудший случай для жертвы)."""
    cands = [(tt, x, y) for tt, x, y in pts if tt <= t and t - tt <= SI_STALE_S]
    if not cands:
        return None
    last_t = cands[-1][0]
    same = [(x, y) for tt, x, y in cands if tt == last_t]
    if near is None or len(same) == 1:
        return same[-1]
    nx, ny = near
    return min(same, key=lambda q: (q[0] - nx) ** 2 + (q[1] - ny) ** 2)


def safety_index(victim_hero: str, victim_team: int, t: int,
                 positions_by_hero: dict[str, list[tuple[int, float, float]]],
                 hero_team: dict[str, int]) -> float:
    """Фактический позиционный риск точки смерти, 0 (безопасно) .. 1.

    0.65 · давление врагов (сумма линейно затухающих по дистанции весов,
    насыщение на SI_PRESSURE_SATURATION «полных» врагах) +
    0.35 · глубина на чужой половине (проекция на диагональ карты).
    hero_team — НОРМАЛИЗОВАННОЕ имя героя → команда.
    """
    vpts = positions_by_hero.get(_normalize_hero(victim_hero))
    vpos = _pos_at(vpts, t) if vpts else None
    if vpos is None:
        return 0.0
    vx, vy = vpos

    pressure = 0.0
    for hero, pts in positions_by_hero.items():
        team = hero_team.get(hero, 0)
        if team == 0 or team == victim_team:
            continue
        epos = _pos_at(pts, t, near=(vx, vy))
        if epos is None:
            continue
        dist = ((epos[0] - vx) ** 2 + (epos[1] - vy) ** 2) ** 0.5
        pressure += max(0.0, 1.0 - dist / SI_PRESSURE_RADIUS)
    pressure = min(1.0, pressure / SI_PRESSURE_SATURATION)

    raw = (vx + vy) / (2.0 * MAP_HALF_DIAG)          # -1 (база R) .. +1 (база D)
    depth = (raw + 1.0) / 2.0 if victim_team == 2 else (1.0 - raw) / 2.0
    depth = min(1.0, max(0.0, depth))

    return round(min(1.0, 0.65 * pressure + 0.35 * depth), 3)


def wp_attribution(points: list[dict], kills: list[dict],
                   hero_player: dict[str, int],
                   player_team: dict[int, int],
                   positions_by_hero: dict[str, list[tuple[int, float, float]]]
                   | None = None,
                   ) -> tuple[dict[int, list[dict]], dict[int, float]]:
    """Оконная атрибуция ΔWP (Гл. 6.1.1) — общая для ошибок и импакта.

    Для каждого минутного окна со сдвигом |ΔWP| >= τ по СГЛАЖЕННОЙ кривой:
    - падение WP команды делится поровну между СМЕРТЯМИ её героев в окне
      (класс critical_death в errors, дебет в импакт жертв);
    - рост WP команды делится поровну между её героями-УБИЙЦАМИ в окне
      (кредит в импакт; вклад c_p бейзлайна — равные доли, веса по
      урону/участию придут с моделью).
    Окно без смертей/убийств соответствующей команды не атрибутируется
    (объективы, фарм — классы вне бейзлайна).

    Возвращает: (errors: pid → [GameError], impact: pid → Σ ΔWP).
    """
    errors: dict[int, list[dict]] = {}
    impact: dict[int, float] = {}
    if len(points) < 2:
        return errors, impact
    smooth = _median3([p["radiant_wp"] for p in points])
    for i in range(1, len(points)):
        t_lo, t_hi = points[i - 1]["game_time"], points[i]["game_time"]
        delta_radiant = smooth[i] - smooth[i - 1]
        if abs(delta_radiant) < CRITICAL_DEATH_TAU:
            continue
        losing_team = 2 if delta_radiant < 0 else 3
        rising_team = 3 if losing_team == 2 else 2

        window = [k for k in kills if t_lo < int(k["game_time"]) <= t_hi]

        deaths = [k for k in window
                  if player_team.get(
                      hero_player.get(str(k["target"]), -1)) == losing_team]
        if deaths:
            share = -abs(delta_radiant) / len(deaths)
            for k in deaths:
                pid = hero_player[str(k["target"])]
                hero = str(k["target"]).replace("npc_dota_hero_", "")
                impact[pid] = impact.get(pid, 0.0) + share
                si = 0.0
                if positions_by_hero:
                    hero_team = {h: player_team.get(p, 0)
                                 for h, p in ((_normalize_hero(hh), pp)
                                              for hh, pp in hero_player.items())}
                    si = safety_index(str(k["target"]), losing_team,
                                      int(k["game_time"]), positions_by_hero,
                                      hero_team)
                note = ""
                if si >= 0.6:
                    note = f" Позиция была рискованной (SI {si:.2f})."
                errors.setdefault(pid, []).append({
                    "type": "critical_death",
                    "game_time": int(k["game_time"]),
                    "delta_wp": round(share, 4),
                    "safety_index": si,
                    "explanation": (
                        f"Смерть {hero} на {_fmt_min(int(k['game_time']))} — "
                        f"вероятность победы команды упала на "
                        f"{abs(share) * 100:.0f}% (окно {t_lo // 60}–{t_hi // 60} мин)."
                        + note),
                })

        killers = {hero_player[str(k["attacker"])]
                   for k in window
                   if player_team.get(
                       hero_player.get(str(k.get("attacker", "")), -1)) == rising_team}
        if killers:
            credit = abs(delta_radiant) / len(killers)
            for pid in killers:
                impact[pid] = impact.get(pid, 0.0) + credit
    return errors, impact


def detect_errors(points: list[dict], kills: list[dict],
                  hero_player: dict[str, int],
                  player_team: dict[int, int]) -> dict[int, list[dict]]:
    """Ошибки игроков (обёртка над wp_attribution, см. её докстринг)."""
    errors, _ = wp_attribution(points, kills, hero_player, player_team)
    return errors


def _turning_point(points: list[dict]) -> dict | None:
    """Точка с максимальным |ΔWP| между соседними минутами.

    Ищется по сглаженной кривой: одиночный выброс модели — не «переломный
    момент» игры. В timeline при этом публикуется сырой выход модели.
    """
    smooth = _median3([p["radiant_wp"] for p in points])
    best, best_delta = None, 0.0
    for i in range(1, len(points)):
        d = smooth[i] - smooth[i - 1]
        if abs(d) > abs(best_delta):
            best, best_delta = points[i], d
    if best is None or abs(best_delta) < 0.08:
        return None
    return {"game_time": best["game_time"], "delta_wp": round(best_delta, 4)}


def _fmt_min(seconds: int) -> str:
    return f"{seconds // 60}-й минуте"


def build_narrative(winner: str, players: list[dict],
                    turning: dict | None) -> str:
    """Шаблонный нарратив-бейзлайн (LLM Service — Фаза 4, Гл. 3.9)."""
    side = "Силы Света (Radiant)" if winner == "Radiant" else "Силы Тьмы (Dire)"
    parts = [f"Победу одержали {side}."]
    if turning:
        direction = ("в пользу Radiant" if turning["delta_wp"] > 0
                     else "в пользу Dire")
        parts.append(
            f"Переломный момент на {_fmt_min(turning['game_time'])}: "
            f"вероятность победы сместилась на "
            f"{abs(turning['delta_wp']) * 100:.0f}% {direction}.")
    if players:
        top = max(players, key=lambda p: float(p.get("gpm", 0)))
        hero = str(top.get("hero", "")).replace("npc_dota_hero_", "")
        parts.append(
            f"Лучший фарм у {top.get('player_name') or hero} "
            f"({hero}, {top.get('gpm', 0):.0f} GPM).")
    return " ".join(parts)


def build_analysis(match_id: int, winner: str, players: list[dict],
                   timeline: dict, model_version: str,
                   kills: list[dict] | None = None,
                   positions: list[dict] | None = None,
                   narrator=None) -> dict:
    """Схема MatchAnalysis (+ hero/player_name — аддитивные поля).

    narrator — опциональный LLMNarrator (narrative.py); None или сбой
    генерации ⇒ шаблонный бейзлайн (Гл. 3.9, деградация).
    """
    points = timeline["points"]
    final_wp = points[-1]["radiant_wp"] if points else 0.5
    turning = _turning_point(points)

    hero_player = {str(p.get("hero", "")): int(p["player_id"])
                   for p in players if p.get("hero")}
    player_team = {int(p["player_id"]): int(p.get("team", 0))
                   for p in players}
    errors, impact = wp_attribution(
        points, kills or [], hero_player, player_team,
        positions_by_hero=index_positions(positions) if positions else None)

    player_entries = [
        {
            "player_id": int(p["player_id"]),
            # steam64 как строка: > 2^53, теряет точность в JSON-числах JS.
            "account_id": str(int(p.get("account_id", 0))),
            "hero_id": hero_id(str(p.get("hero", ""))),
            "lane": p.get("lane", ""),
            "hero": p.get("hero", ""),
            "player_name": p.get("player_name", ""),
            "gpm": float(p.get("gpm", 0)),
            "laning_score": _laning_score(p),
            "impact_score": _impact_score(impact.get(int(p["player_id"]), 0.0)),
            "delta_wp_sum": round(impact.get(int(p["player_id"]), 0.0), 4),
            "errors": errors.get(int(p["player_id"]), []),
        }
        for p in sorted(players, key=lambda x: int(x["player_id"]))
    ]

    narrative = None
    if narrator is not None:
        narrative = narrator.generate(winner, final_wp, turning, player_entries)
    narrative_source = "llm" if narrative else "template"
    if narrative is None:
        narrative = build_narrative(winner, players, turning)

    return {
        "match_id": match_id,
        "status": "completed",
        "win_probability": {"final_radiant": final_wp},
        "players": player_entries,
        "narrative": narrative,
        "narrative_source": narrative_source,
        # partial: laning/impact — прокси, ошибки — rule-based
        # ΔWP-бейзлайн (без Safety Index и классов, кроме critical_death) —
        # независимо от того, LLM нарратив или шаблон.
        "partial": True,
        "report_version": REPORT_VERSION,
        "model_version": model_version,
    }
