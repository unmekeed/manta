"""Датасет Win Probability из MatchTimelineFeatures (Гл. 6.2.2, Гл. 7.2).

Каждая строка датасета — снапшот матча в минуту t; target — radiant_win.
Снапшоты одного матча сильно скоррелированы, поэтому сплит train/valid
делается ПО МАТЧАМ (group split) — иначе валидация подсмотрит исход
из соседних минут того же матча (leakage).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass

import numpy as np
import requests

FEATURES = [
    "game_time",
    "networth_diff",
    "xp_diff",
    "kills_diff",
    "kills_total",
    "position_advance",   # территориальное продвижение [-1,1] (миграция 005)
]


PRO_TIER = "Professional"


@dataclass
class Dataset:
    X: np.ndarray            # (n, len(FEATURES))
    y: np.ndarray            # (n,) 0/1
    groups: np.ndarray       # (n,) match_id — для group split
    n_matches: int
    n_synthetic: int = 0
    # tier строки ('' для старых/синтетических данных). Матчи
    # PRO_TIER — эталонный holdout: НИКОГДА не попадают в train/valid.
    tiers: np.ndarray | None = None

    def _tier_mask(self, tier: str) -> np.ndarray:
        if self.tiers is None:
            return np.zeros(len(self.y), dtype=bool)
        return self.tiers == tier

    def benchmark(self) -> tuple[np.ndarray, np.ndarray]:
        """Эталонная выборка (матчи про-команд)."""
        m = self._tier_mask(PRO_TIER)
        return self.X[m], self.y[m]

    def split_by_match(self, valid_frac: float = 0.2, seed: int = 42):
        """Group split по НЕэталонным матчам: матч целиком в train или valid."""
        pro = self._tier_mask(PRO_TIER)
        rng = random.Random(seed)
        matches = sorted(set(self.groups[~pro].tolist()))
        rng.shuffle(matches)
        n_valid = max(1, int(len(matches) * valid_frac))
        valid_set = set(matches[:n_valid])
        in_valid = np.array([g in valid_set for g in self.groups])
        tr = ~in_valid & ~pro
        va = in_valid & ~pro
        return (self.X[tr], self.y[tr]), (self.X[va], self.y[va])


def row_to_features(row: dict) -> list[float]:
    kills_r = float(row["kills_radiant"])
    kills_d = float(row["kills_dire"])
    return [
        float(row["game_time"]),
        float(row["networth_diff"]),
        float(row["xp_diff"]),
        kills_r - kills_d,
        kills_r + kills_d,
        float(row.get("position_advance", 0.0)),
    ]


def load_from_clickhouse(url: str, database: str, user: str, password: str) -> Dataset:
    """Все матчи из MatchTimelineFeatures (FINAL — свежая версия фич)."""
    resp = requests.post(
        url,
        params={"database": database, "default_format": "JSONEachRow"},
        data="SELECT match_id, game_time, networth_diff, xp_diff,"
             "       kills_radiant, kills_dire, position_advance,"
             "       radiant_win, tier"
             "  FROM MatchTimelineFeatures FINAL ORDER BY match_id, game_time",
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        timeout=120,
    )
    resp.raise_for_status()
    rows = [json.loads(line) for line in resp.text.splitlines() if line]
    if not rows:
        return Dataset(X=np.empty((0, len(FEATURES))), y=np.empty(0),
                       groups=np.empty(0), n_matches=0)
    X = np.array([row_to_features(r) for r in rows], dtype=np.float64)
    y = np.array([int(r["radiant_win"]) for r in rows], dtype=np.int64)
    groups = np.array([int(r["match_id"]) for r in rows], dtype=np.int64)
    tiers = np.array([str(r.get("tier", "")) for r in rows])
    return Dataset(X=X, y=y, groups=groups,
                   n_matches=len(set(groups.tolist())), tiers=tiers)


# -- Синтетические матчи ------------------------------------------------------
# Для smoke-прогонов конвейера, пока реальных матчей мало. Генератор имитирует
# основную закономерность: команда с растущим преимуществом по net worth/XP
# и убийствам выигрывает с вероятностью sigmoid от масштаба преимущества.

def synth_matches(n: int, seed: int = 7) -> Dataset:
    rng = np.random.default_rng(seed)
    Xs, ys, gs = [], [], []
    for i in range(n):
        match_id = 1000 + i  # синтетический диапазон, в ClickHouse не пишется
        duration = int(rng.uniform(1500, 3600))
        drift = rng.normal(0.0, 12.0)      # золото/с в пользу Radiant
        noise = rng.normal(0.0, 900.0, size=duration // 60)
        radiant_win = None
        nw = xp = 0.0
        kills_r = kills_d = 0
        for j, t in enumerate(range(60, duration + 1, 60)):
            nw = drift * t + noise[min(j, len(noise) - 1)] * math.sqrt(t / 600)
            xp = nw * rng.uniform(1.0, 1.4)
            if rng.random() < 0.35:
                if nw >= 0:
                    kills_r += int(rng.integers(0, 3))
                    kills_d += int(rng.integers(0, 2))
                else:
                    kills_r += int(rng.integers(0, 2))
                    kills_d += int(rng.integers(0, 3))
            adv = max(-1.0, min(1.0, nw / 25000.0 + rng.normal(0, 0.15)))
            Xs.append([t, nw, xp, kills_r - kills_d, kills_r + kills_d, adv])
            gs.append(match_id)
        p_radiant = 1.0 / (1.0 + math.exp(-nw / 8000.0))
        radiant_win = 1 if rng.random() < p_radiant else 0
        ys.extend([radiant_win] * (duration // 60))
    return Dataset(X=np.array(Xs), y=np.array(ys), groups=np.array(gs),
                   n_matches=n, n_synthetic=n)


def merge(a: Dataset, b: Dataset) -> Dataset:
    if a.X.size == 0:
        return b
    if b.X.size == 0:
        return a
    tiers_a = a.tiers if a.tiers is not None else np.array([""] * len(a.y))
    tiers_b = b.tiers if b.tiers is not None else np.array([""] * len(b.y))
    return Dataset(
        X=np.vstack([a.X, b.X]),
        y=np.concatenate([a.y, b.y]),
        groups=np.concatenate([a.groups, b.groups]),
        n_matches=a.n_matches + b.n_matches,
        n_synthetic=a.n_synthetic + b.n_synthetic,
        tiers=np.concatenate([tiers_a, tiers_b]),
    )


def dataset_hash(ds: Dataset) -> str:
    """Отпечаток датасета для метаданных артефакта (аналог DVC-хэша)."""
    h = hashlib.sha256()
    h.update(ds.X.tobytes())
    h.update(ds.y.tobytes())
    return h.hexdigest()[:16]
