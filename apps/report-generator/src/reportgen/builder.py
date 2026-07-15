"""Сборка отчёта по матчу — чистые функции (контракты Гл. 7: Timeline,
MatchAnalysis).

Вход — строки витрин ClickHouse и WP-кривая от ML Service; выход —
готовые JSON-объекты для MatchReports. Поля laning_score/impact_score —
детерминированные бейзлайн-прокси (честные модели Laning Evaluator и
Impact придут в следующих спринтах, Гл. 6.2.1); отчёт помечен
partial=true, пока список ошибок пуст и нарратив шаблонный (не LLM).
"""
from __future__ import annotations

REPORT_VERSION = "0.1.0"


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


def _laning_score(p: dict) -> float:
    """Бейзлайн лейнинга: ласт-хиты и денаи к 10-й минуте, нормированные
    к типичному максимуму кора (~80 LH). Диапазон 0..1."""
    raw = (float(p.get("lh_at_10", 0)) + 2.0 * float(p.get("dn_at_10", 0))) / 80.0
    return round(min(max(raw, 0.0), 1.0), 3)


def _impact_score(p: dict) -> float:
    """Бейзлайн импакта: доля в net worth команды, отмасштабированная так,
    что равная доля (0.2) даёт 0.5. Честный Impact Score = сумма ΔWP
    игрока (Гл. 6.1.3) — после Error Detection Engine."""
    share = float(p.get("gold_share", 0.0))
    return round(min(max(share / 0.4, 0.0), 1.0), 3)


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


def detect_errors(points: list[dict], kills: list[dict],
                  hero_player: dict[str, int],
                  player_team: dict[int, int]) -> dict[int, list[dict]]:
    """Rule-based бейзлайн Error Detection Engine (Гл. 6.2.1).

    Атрибуция ΔWP (Гл. 6.1.1): для каждого минутного окна, где WP команды
    упала сильнее τ по СГЛАЖЕННОЙ кривой, падение распределяется поровну
    между смертями героев этой команды в окне (вклад c_p бейзлайна —
    равные доли; веса по урону/участию придут с моделью). Окно без
    смертей пострадавшей команды ошибок не порождает: падение вызвано
    не гибелью (потеря объектива и т.п.) — такие классы добавятся позже.

    Возвращает: player_id → [{type, game_time, delta_wp, safety_index,
    explanation}] (схема GameError, Гл. 7).
    """
    if len(points) < 2:
        return {}
    smooth = _median3([p["radiant_wp"] for p in points])
    errors: dict[int, list[dict]] = {}
    for i in range(1, len(points)):
        t_lo, t_hi = points[i - 1]["game_time"], points[i]["game_time"]
        delta_radiant = smooth[i] - smooth[i - 1]
        if abs(delta_radiant) < CRITICAL_DEATH_TAU:
            continue
        losing_team = 2 if delta_radiant < 0 else 3
        window_deaths = [
            k for k in kills
            if t_lo < int(k["game_time"]) <= t_hi
            and player_team.get(
                hero_player.get(str(k["target"]), -1)) == losing_team
        ]
        if not window_deaths:
            continue
        share = -abs(delta_radiant) / len(window_deaths)
        for k in window_deaths:
            pid = hero_player[str(k["target"])]
            hero = str(k["target"]).replace("npc_dota_hero_", "")
            errors.setdefault(pid, []).append({
                "type": "critical_death",
                "game_time": int(k["game_time"]),
                "delta_wp": round(share, 4),
                "safety_index": 0.0,  # позиционный риск — после Гл. 6.1.2
                "explanation": (
                    f"Смерть {hero} на {_fmt_min(int(k['game_time']))} — "
                    f"вероятность победы команды упала на "
                    f"{abs(share) * 100:.0f}% (окно {t_lo // 60}–{t_hi // 60} мин)."),
            })
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
                   kills: list[dict] | None = None) -> dict:
    """Схема MatchAnalysis (+ hero/player_name — аддитивные поля)."""
    points = timeline["points"]
    final_wp = points[-1]["radiant_wp"] if points else 0.5
    turning = _turning_point(points)

    hero_player = {str(p.get("hero", "")): int(p["player_id"])
                   for p in players if p.get("hero")}
    player_team = {int(p["player_id"]): int(p.get("team", 0))
                   for p in players}
    errors = detect_errors(points, kills or [], hero_player, player_team)

    return {
        "match_id": match_id,
        "status": "completed",
        "win_probability": {"final_radiant": final_wp},
        "players": [
            {
                "player_id": int(p["player_id"]),
                "hero_id": 0,  # числовые ID героев — после словаря героев
                "hero": p.get("hero", ""),
                "player_name": p.get("player_name", ""),
                "laning_score": _laning_score(p),
                "impact_score": _impact_score(p),
                "errors": errors.get(int(p["player_id"]), []),
            }
            for p in sorted(players, key=lambda x: int(x["player_id"]))
        ],
        "narrative": build_narrative(winner, players, turning),
        # partial: нарратив шаблонный, laning/impact — прокси, ошибки —
        # rule-based ΔWP-бейзлайн (без Safety Index и классов, кроме
        # critical_death).
        "partial": True,
        "report_version": REPORT_VERSION,
        "model_version": model_version,
    }
