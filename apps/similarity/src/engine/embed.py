"""Эмбеддинги матчей для Similarity Engine (Гл. 3, SimilarityService).

Матч кодируется конкатенацией трёх блоков (каждый L2-нормируется отдельно,
чтобы ни один не доминировал в косинусе):

1. **Траектория** — кривые networth_diff / xp_diff / kills_diff,
   ресэмплированные на фиксированную сетку долей длительности матча
   (RESAMPLE_POINTS точек): «как разворачивалась игра» безотносительно
   её длины. Диффы нормируются на суммарную экономику момента (масштаб
   поздней игры не забивает раннюю).
2. **Мета** — длительность, суммарные убийства, снесённые башни
   (нормированные грубыми константами масштаба).
3. **Составы** — multi-hot по hero_id обеих сторон (Radiant в первой
   половине, Dire во второй): похожие драфты сближают матчи.

Точный косинус-поиск по ~10^3–10^4 матчей — миллисекунды; ANN-индексы
(Гл. 3: FAISS) подключаются при 10^5+, интерфейс index.py их переживёт.
"""
from __future__ import annotations

import numpy as np

RESAMPLE_POINTS = 12
N_HEROES = 152          # запас по hero_id (сейчас максимум ~140)
CURVES = ("networth_diff", "xp_diff", "kills_diff")

# Грубые масштабы для мета-блока.
DURATION_SCALE_S = 3600.0
KILLS_SCALE = 60.0
TOWERS_SCALE = 11.0

EMBED_DIM = RESAMPLE_POINTS * len(CURVES) + 3 + 2 * N_HEROES


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def _resample(values: list[float], k: int = RESAMPLE_POINTS) -> np.ndarray:
    """Линейный ресэмплинг кривой на k точек долей длительности."""
    if not values:
        return np.zeros(k)
    src = np.asarray(values, dtype=float)
    if len(src) == 1:
        return np.full(k, src[0])
    x_src = np.linspace(0.0, 1.0, len(src))
    x_dst = np.linspace(0.0, 1.0, k)
    return np.interp(x_dst, x_src, src)


def embed_match(timeline: list[dict], hero_ids_radiant: list[int],
                hero_ids_dire: list[int]) -> np.ndarray:
    """Эмбеддинг матча из строк MTF (отсортированы по game_time) и составов.

    timeline: [{"game_time", "networth_diff", "xp_diff", "kills_radiant",
                "kills_dire", "networth_total"?, "towers_diff"?}, ...]
    """
    rows = sorted(timeline, key=lambda r: int(r["game_time"]))

    def _norm_curve(key: str) -> list[float]:
        out = []
        for r in rows:
            if key == "kills_diff":
                v = float(r["kills_radiant"]) - float(r["kills_dire"])
                scale = max(float(r["kills_radiant"]) + float(r["kills_dire"]), 8.0)
            else:
                v = float(r[key])
                total = r.get("networth_total")
                scale = (float(total) if total is not None and total == total
                         and float(total) > 0
                         else 12000.0 + int(r["game_time"]) * 18.0)
            out.append(v / scale)
        return out

    shape = np.concatenate([_resample(_norm_curve(c)) for c in CURVES])

    last = rows[-1] if rows else {}
    kills_final = (float(last.get("kills_radiant", 0))
                   + float(last.get("kills_dire", 0)))
    towers = last.get("towers_diff")
    towers_f = float(towers) if towers is not None and towers == towers else 0.0
    meta = np.array([
        (int(last.get("game_time", 0))) / DURATION_SCALE_S,
        kills_final / KILLS_SCALE,
        towers_f / TOWERS_SCALE,
    ])

    heroes = np.zeros(2 * N_HEROES)
    for hid in hero_ids_radiant:
        if 0 <= int(hid) < N_HEROES:
            heroes[int(hid)] = 1.0
    for hid in hero_ids_dire:
        if 0 <= int(hid) < N_HEROES:
            heroes[N_HEROES + int(hid)] = 1.0

    return np.concatenate([_l2(shape), _l2(meta), _l2(heroes)])


def cosine_top_k(query: np.ndarray, matrix: np.ndarray, k: int,
                 exclude: int | None = None) -> list[tuple[int, float]]:
    """Топ-k строк matrix по косинусу к query; exclude — индекс строки-себя."""
    q = _l2(np.asarray(query, dtype=float))
    m_norm = np.linalg.norm(matrix, axis=1)
    m_norm[m_norm < 1e-12] = 1.0
    scores = (matrix @ q) / m_norm
    if exclude is not None and 0 <= exclude < len(scores):
        scores[exclude] = -np.inf
    k = max(1, min(k, len(scores)))
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return [(int(i), float(scores[i])) for i in idx if np.isfinite(scores[i])]


def match_document(match_id: int, timeline: list[dict],
                   heroes_radiant: list[str], heroes_dire: list[str]) -> str:
    """Текстовый документ матча для RAG (RetrieveContext): компактная
    сводка исхода и сюжета — то, что LLM-Coach процитирует как прецедент."""
    rows = sorted(timeline, key=lambda r: int(r["game_time"]))
    if not rows:
        return f"match {match_id}: нет таймлайна"
    last = rows[-1]
    dur_min = int(last["game_time"]) // 60
    win = "Radiant" if int(last.get("radiant_win", 0)) == 1 else "Dire"
    diffs = [float(r["networth_diff"]) for r in rows]
    max_lead, min_lead = max(diffs), min(diffs)
    comeback = ((win == "Radiant" and min_lead < -8000)
                or (win == "Dire" and max_lead > 8000))
    plot = "камбэк" if comeback else (
        "доминация" if (max_lead > 8000 if win == "Radiant"
                        else min_lead < -8000) else "равная игра")
    kills = f"{int(last['kills_radiant'])}:{int(last['kills_dire'])}"
    her_r = ", ".join(h.replace("npc_dota_hero_", "") for h in heroes_radiant)
    her_d = ", ".join(h.replace("npc_dota_hero_", "") for h in heroes_dire)
    return (f"Матч {match_id}: победа {win} за {dur_min} мин, счёт {kills}, "
            f"сюжет: {plot}. Radiant: {her_r or '—'}. Dire: {her_d or '—'}. "
            f"Пик преимущества Radiant {int(max_lead)}, Dire {int(-min_lead)}.")
