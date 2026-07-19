"""Рекомендации драфта поверх DraftStats (аддитивная лог-оддс модель).

predicted_winrate_radiant(state) = sigmoid(Σ вкладов):
- соло-вклады героев Radiant минус Dire;
- синергии пар внутри каждой команды;
- контрпики направленных пар Radiant→Dire.

Все вклады центрируются вычитанием нейтрального лог-оддса (0) через
сглаживание к 0.5, поэтому пустой драфт даёт ровно 0.5. Каждый вклад
масштабируется WEIGHT_* — суммы десятков слабых сигналов не должны
взрываться к 0/1 (бейзлайн обязан быть скромным в уверенности).
"""
from __future__ import annotations

import math

from .stats import DraftStats

WEIGHT_SOLO = 0.6
WEIGHT_PAIR = 0.3
WEIGHT_COUNTER = 0.3
TOP_SUGGESTIONS = 5


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def predicted_winrate_radiant(stats: DraftStats, radiant: list[int],
                              dire: list[int]) -> float:
    lo = 0.0
    for h in radiant:
        lo += WEIGHT_SOLO * stats.solo_lo(h)
    for h in dire:
        lo -= WEIGHT_SOLO * stats.solo_lo(h)
    for i, a in enumerate(radiant):
        for b in radiant[i + 1:]:
            lo += WEIGHT_PAIR * stats.pair_lo(a, b)
    for i, a in enumerate(dire):
        for b in dire[i + 1:]:
            lo -= WEIGHT_PAIR * stats.pair_lo(a, b)
    for h in radiant:
        for v in dire:
            lo += WEIGHT_COUNTER * stats.counter_lo(h, v)
    return _sigmoid(lo)


def _reason(stats: DraftStats, cand: int, allies: list[int],
            enemies: list[int]) -> str:
    """Главный источник вклада кандидата — для поля reason."""
    parts = [(abs(stats.solo_lo(cand)), f"соло-винрейт ({stats.games_of(cand):.0f} игр)")]
    for a in allies:
        parts.append((abs(stats.pair_lo(cand, a)), f"синергия с hero_{a}"))
    for e in enemies:
        parts.append((abs(stats.counter_lo(cand, e)), f"матчап против hero_{e}"))
    parts.sort(reverse=True)
    top = parts[0][1] if parts else "нет данных"
    return f"основной сигнал: {top}; выборка {stats.n_matches} матчей"


def suggest(stats: DraftStats, radiant: list[int], dire: list[int],
            bans: list[int], next_action: str = "",
            top_k: int = TOP_SUGGESTIONS) -> tuple[float, list[dict], str]:
    """(winrate_radiant текущего драфта, топ-k пиков, действующая сторона).

    next_action: 'pick_radiant' | 'pick_dire'; пусто — сторона с меньшим
    числом пиков (Radiant при равенстве).
    """
    side = next_action or ("pick_radiant" if len(radiant) <= len(dire)
                           else "pick_dire")
    acting_radiant = side != "pick_dire"

    base = predicted_winrate_radiant(stats, radiant, dire)
    taken = set(radiant) | set(dire) | set(bans)
    candidates = [h for h in stats.solo if h not in taken]

    scored = []
    for cand in candidates:
        if acting_radiant:
            wr = predicted_winrate_radiant(stats, radiant + [cand], dire)
            gain = wr - base
        else:
            wr_r = predicted_winrate_radiant(stats, radiant, dire + [cand])
            wr = 1.0 - wr_r           # expected_winrate действующей стороны
            gain = base - wr_r
        scored.append((gain, cand, wr))
    scored.sort(reverse=True)

    allies = radiant if acting_radiant else dire
    enemies = dire if acting_radiant else radiant
    suggestions = [
        {"hero_id": cand, "expected_winrate": round(wr, 4),
         "reason": _reason(stats, cand, allies, enemies)}
        for _, cand, wr in scored[:top_k]
    ]
    return base, suggestions, side
