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
    "alive_diff",         # живые герои R−D (миграция 008; NaN у JSON-матчей)
    "towers_diff",        # снесённые башни R−D накопительно (миграция 008)
    "rax_diff",           # снесённые бараки R−D накопительно (миграция 008)
    "networth_rel",       # доля преимущества: networth_diff/networth_total
]

# Фичи, меняющие знак при зеркалировании сторон Radiant↔Dire (все
# разностные и территориальные). game_time и kills_total симметричны.
MIRROR_NEGATE = {"networth_diff", "xp_diff", "kills_diff", "position_advance",
                 "alive_diff", "towers_diff", "rax_diff", "networth_rel"}


PRO_TIER = "Professional"


def mirror_xy(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Зеркальная аугментация (Гл. 6.2.2): к каждому снапшоту добавляется
    его отражение Radiant↔Dire — разностные фичи меняют знак, метка
    инвертируется. Win Probability обязана быть симметрична по сторонам:
    WP(state) = 1 - WP(mirror(state)). Аугментация обнуляет приор стороны
    (в высокоранговых пабликах Radiant выигрывает ~58% — сдвиг относительно
    про-матчей ~45%, из-за которого модель на пабликах смещена).
    """
    Xm = X.copy()
    for i, f in enumerate(FEATURES):
        if f in MIRROR_NEGATE:
            Xm[:, i] = -Xm[:, i]
    return np.vstack([X, Xm]), np.concatenate([y, 1 - y])


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

    def _valid_mask(self, valid_frac: float = 0.2, seed: int = 42):
        """Маска валидационных строк (group split по НЕэталонным матчам)
        и маска эталона. Детерминирована по seed — один и тот же holdout
        воспроизводится и в train, и в гейте."""
        pro = self._tier_mask(PRO_TIER)
        rng = random.Random(seed)
        matches = sorted(set(self.groups[~pro].tolist()))
        rng.shuffle(matches)
        n_valid = max(1, int(len(matches) * valid_frac))
        valid_set = set(matches[:n_valid])
        in_valid = np.array([g in valid_set for g in self.groups])
        return in_valid, pro

    def split_by_match(self, valid_frac: float = 0.2, seed: int = 42):
        """Group split по НЕэталонным матчам: матч целиком в train или valid."""
        in_valid, pro = self._valid_mask(valid_frac, seed)
        tr = ~in_valid & ~pro
        va = in_valid & ~pro
        return (self.X[tr], self.y[tr]), (self.X[va], self.y[va])

    def eval_holdout(self, min_bench_matches: int = 15,
                     valid_frac: float = 0.2, seed: int = 42):
        """Сопоставимый holdout для честного сравнения версий гейтом.

        На нём оцениваются ОБЕ модели (кандидат и production) — так метрика
        считается на одной популяции, а не на разных сплитах разных версий.
        Приоритет — про-эталон (фиксированная выборка tier-1). Если про-матчей
        мало, берётся валидационный сплит с тем же seed, что использует train,
        поэтому кандидат его не видел при обучении.

        Возвращает (X, y, groups, kind), kind ∈ {"benchmark_pro", "valid"}.
        """
        pro = self._tier_mask(PRO_TIER)
        pro_matches = sorted(set(self.groups[pro].tolist()))
        if len(pro_matches) >= min_bench_matches:
            return self.X[pro], self.y[pro], self.groups[pro], "benchmark_pro"
        in_valid, _ = self._valid_mask(valid_frac, seed)
        va = in_valid & ~pro
        return self.X[va], self.y[va], self.groups[va], "valid"


def row_to_features(row: dict) -> list[float]:
    kills_r = float(row["kills_radiant"])
    kills_d = float(row["kills_dire"])
    # Отсутствующие фичи = NaN — нативный пропуск LightGBM, НЕ 0 (ноль —
    # ложный сигнал «ровно посередине»). ClickHouse отдаёт NaN как null →
    # None: position_advance/alive_diff нет у JSON-матчей, alive/towers/rax
    # нет у строк, собранных до миграции 008.
    def _f(key: str) -> float:
        v = row.get(key)
        return float(v) if v is not None else math.nan

    # networth_rel — производная при загрузке: доля преимущества вместо
    # абсолюта (5k на 10-й и на 40-й минуте — разные вселенные). NaN, если
    # networth_total неизвестен (старые строки).
    total = _f("networth_total")
    rel = (float(row["networth_diff"]) / total
           if total == total and total > 0 else math.nan)
    return [
        float(row["game_time"]),
        float(row["networth_diff"]),
        float(row["xp_diff"]),
        kills_r - kills_d,
        kills_r + kills_d,
        _f("position_advance"),
        _f("alive_diff"),
        _f("towers_diff"),
        _f("rax_diff"),
        rel,
    ]


def load_from_clickhouse(url: str, database: str, user: str, password: str) -> Dataset:
    """Все матчи из MatchTimelineFeatures (FINAL — свежая версия фич)."""
    resp = requests.post(
        url,
        params={"database": database, "default_format": "JSONEachRow"},
        data="SELECT match_id, game_time, networth_diff, networth_total,"
             "       xp_diff, kills_radiant, kills_dire, position_advance,"
             "       alive_diff, towers_diff, rax_diff,"
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
            # редкие тимфайты/пуши: живые и здания коррелируют с преимуществом
            alive = float(int(max(-5, min(5, rng.normal(nw / 12000.0, 1.2)))))
            towers = float(int(max(-11, min(11, nw / 9000.0 + rng.normal(0, 0.7)))))
            rax = float(int(max(-6, min(6, nw / 20000.0 + rng.normal(0, 0.3)))))
            total = 12000.0 + t * 18.0   # рост суммарной экономики
            Xs.append([t, nw, xp, kills_r - kills_d, kills_r + kills_d, adv,
                       alive, towers, rax, nw / total])
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
