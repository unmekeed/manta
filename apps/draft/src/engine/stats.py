"""Статистика драфта из PlayerMatchFeatures (Гл. 3: Draft Engine, бейзлайн).

GNN из спеки требует 10^4+ матчей; на текущем масштабе честный бейзлайн —
частотные оценки с байесовским сглаживанием:

- **соло-винрейт** героя;
- **синергия** пары в одной команде;
- **контрпик** направленной пары (h против h').

Сглаживание — Beta-prior к глобальному 0.5: (wins + 0.5·m) / (games + m).
Пары на ~10^3 матчей очень разрежены (единицы наблюдений) — тяжёлый prior
(M_PAIR) сознательно прижимает оценки к нейтральным: модель не выдумывает
уверенность, которой нет в данных. С ростом датасета оценки оживают сами.

Вклады считаются в лог-оддсах и суммируются (аддитивная модель), итоговая
вероятность — сигмоида суммы.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

M_SOLO = 40.0    # prior-вес соло-винрейта (наблюдений на героя ~десятки)
M_PAIR = 60.0    # prior-вес пар (наблюдений на пару — единицы)
PRIOR = 0.5


def _smoothed(wins: float, games: float, m: float) -> float:
    return (wins + PRIOR * m) / (games + m)


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


@dataclass
class DraftStats:
    solo: dict[int, tuple[float, float]] = field(default_factory=dict)
    pair: dict[tuple[int, int], tuple[float, float]] = field(default_factory=dict)
    counter: dict[tuple[int, int], tuple[float, float]] = field(default_factory=dict)
    n_matches: int = 0

    # -- оценки (лог-оддсы вклада) -------------------------------------------

    def solo_lo(self, h: int) -> float:
        w, g = self.solo.get(h, (0.0, 0.0))
        return _logit(_smoothed(w, g, M_SOLO))

    def pair_lo(self, a: int, b: int) -> float:
        w, g = self.pair.get((min(a, b), max(a, b)), (0.0, 0.0))
        return _logit(_smoothed(w, g, M_PAIR))

    def counter_lo(self, h: int, versus: int) -> float:
        """Лог-оддсы победы h в матчах против versus."""
        w, g = self.counter.get((h, versus), (0.0, 0.0))
        return _logit(_smoothed(w, g, M_PAIR))

    def games_of(self, h: int) -> float:
        return self.solo.get(h, (0.0, 0.0))[1]


def build_stats(player_rows: list[dict], hero_id_of) -> DraftStats:
    """Собрать статистику из строк PMF: {"match_id","team","hero","won"}.

    hero_id_of: npc-имя → числовой id (0 = неизвестный, пропускается).
    """
    by_match: dict[int, dict[int, list[tuple[int, int]]]] = defaultdict(
        lambda: {2: [], 3: []})
    for r in player_rows:
        hid = int(hero_id_of(str(r.get("hero") or "")))
        if hid <= 0:
            continue
        by_match[int(r["match_id"])][int(r["team"])].append(
            (hid, int(r.get("won") or 0)))

    solo: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    pair: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    counter: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])

    n = 0
    for teams in by_match.values():
        r_heroes, d_heroes = teams.get(2, []), teams.get(3, [])
        if not r_heroes or not d_heroes:
            continue
        n += 1
        for side in (r_heroes, d_heroes):
            for i, (h, won) in enumerate(side):
                solo[h][0] += won
                solo[h][1] += 1
                for h2, _ in side[i + 1:]:
                    key = (min(h, h2), max(h, h2))
                    pair[key][0] += won
                    pair[key][1] += 1
        for h, won in r_heroes:
            for h2, _ in d_heroes:
                counter[(h, h2)][0] += won
                counter[(h, h2)][1] += 1
                counter[(h2, h)][0] += 1 - won
                counter[(h2, h)][1] += 1

    return DraftStats(
        solo={h: (w, g) for h, (w, g) in solo.items()},
        pair={k: (w, g) for k, (w, g) in pair.items()},
        counter={k: (w, g) for k, (w, g) in counter.items()},
        n_matches=n,
    )
