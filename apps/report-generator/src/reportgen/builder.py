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
                   timeline: dict, model_version: str) -> dict:
    """Схема MatchAnalysis (+ hero/player_name — аддитивные поля)."""
    points = timeline["points"]
    final_wp = points[-1]["radiant_wp"] if points else 0.5
    turning = _turning_point(points)
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
                "errors": [],  # Error Detection Engine — Гл. 6.2.1, позже
            }
            for p in sorted(players, key=lambda x: int(x["player_id"]))
        ],
        "narrative": build_narrative(winner, players, turning),
        # partial: ошибки не детектируются, нарратив шаблонный.
        "partial": True,
        "report_version": REPORT_VERSION,
        "model_version": model_version,
    }
