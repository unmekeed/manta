"""Мета героев — чистые функции (спринт 31, бейзлайн Meta Engine).

Агрегаты по героям из PlayerMatchFeatures сглаживаются к 0.5:
у героя с 2 матчами «100% винрейта» — шум, а не сигнал. Классическое
байесовское сглаживание (бета-приор, эквивалент K виртуальных матчей
с винрейтом 0.5): чем меньше матчей, тем сильнее оценка прижата к 0.5.

Эти же сглаженные винрейты — будущий вход Draft Engine (Гл. 3.9):
draft advantage = Σ shrunk_winrate(пики Radiant) − Σ (пики Dire).
"""
from __future__ import annotations

SHRINK_K = 10  # виртуальных матчей приора; ~вес одного вечера игр


def shrunk_winrate(wins: int, matches: int, k: int = SHRINK_K) -> float:
    """Винрейт, прижатый к 0.5 при малой выборке."""
    return (wins + k * 0.5) / (matches + k)


def build_meta_rows(hero_rows: list[dict], total_matches: int) -> list[dict]:
    """Строки MetaHeroes из агрегатов ClickHouse.

    hero_rows: [{hero, matches, wins, avg_gpm}], total_matches — всего
    матчей в витрине (для pick_rate).
    """
    out = []
    for r in hero_rows:
        hero = str(r.get("hero", ""))
        if not hero:
            continue
        matches, wins = int(r["matches"]), int(r["wins"])
        out.append({
            "hero": hero,
            "matches": matches,
            "wins": wins,
            "winrate": round(wins / matches, 4) if matches else 0.0,
            "shrunk_winrate": round(shrunk_winrate(wins, matches), 4),
            "pick_rate": (round(matches / total_matches, 4)
                          if total_matches else 0.0),
            "avg_gpm": float(r.get("avg_gpm", 0.0)),
        })
    return out
