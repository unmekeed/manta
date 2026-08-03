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
import os
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
    # A14 (спринт 83). Только из реплея, у JSON-матчей NaN.
    "local_manpower_diff",  # перевес В ТОЧКЕ КОНТАКТА, не по всей карте
    "spread_diff",          # собранность команды: разброс R − D
    # Трек F (миграция 012). Все NaN у матчей, собранных до трека —
    # LightGBM обрабатывает пропуск нативно.
    "roshan_diff",        # убийства Рошана R−D накопительно (F2)
    "aegis_alive",        # у кого сейчас аегис: −1/0/+1 (F2)
    "buybacks_diff",      # потрачено бэйбеков R−D (F2)
    "first_blood",        # сторона первой крови (F2)
    "item_value_diff",    # стоимость закупа R−D (F4)
    "key_items_diff",     # взято ключевых предметов R−D (F4)
    "obs_wards_diff",     # активных обс-вардов R−D (F5)
    "vision_coverage_diff",  # ДОЛЯ КАРТЫ под обзором R−D (волна 1, спринт 90)
    "sen_wards_diff",     # поставлено сентри R−D (F5)
    "runes_diff",         # подобрано рун R−D (F5)
    "neutral_tier_diff",  # сумма тиров нейтралок R−D (F6)
    "levels_diff",        # сумма уровней героев R−D (F6)
    "draft_prior",        # P(win) по составам, Draft Prior Model (F3)
]

# Фичи, меняющие знак при зеркалировании сторон Radiant↔Dire (все
# разностные и территориальные). game_time и kills_total симметричны.
MIRROR_NEGATE = {"networth_diff", "xp_diff", "kills_diff", "position_advance",
                 "alive_diff", "towers_diff", "rax_diff", "networth_rel",
                 # A14: обе разностные, при смене сторон меняют знак
                 "local_manpower_diff", "spread_diff",
                 # Трек F: все разностные и знаковые фичи меняют знак.
                 "roshan_diff", "aegis_alive", "buybacks_diff", "first_blood",
                 "item_value_diff", "key_items_diff", "obs_wards_diff",
                 "vision_coverage_diff",
                 "sen_wards_diff", "runes_diff", "neutral_tier_diff",
                 "levels_diff"}

# draft_prior — вероятность, а не разность: при зеркалировании сторон
# она переходит в 1 − p, а не в −p. Обрабатывается отдельно в mirror_xy.
MIRROR_COMPLEMENT = {"draft_prior"}


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
        elif f in MIRROR_COMPLEMENT:
            Xm[:, i] = 1.0 - Xm[:, i]
    return np.vstack([X, Xm]), np.concatenate([y, 1 - y])


@dataclass
class Dataset:
    X: np.ndarray            # (n, len(FEATURES))
    y: np.ndarray            # (n,) 0/1
    groups: np.ndarray       # (n,) match_id — для group split
    n_matches: int
    n_synthetic: int = 0
    # tier строки ('' для старых/синтетических данных). Про-матчи делятся
    # между train и эталоном (PRO_TRAIN_FRAC) — см. _bench_mask.
    tiers: np.ndarray | None = None
    # id патча OpenDota по строкам (0 — неизвестен: старые строки до
    # миграции 010). Используется patch_weights().
    patches: np.ndarray | None = None

    def patch_weights(self) -> np.ndarray:
        """A9: множитель веса строки по возрасту патча.

        После баланс-патча мета меняется, но выбрасывать старые матчи —
        терять данные. Компромисс: вес PATCH_OLD_WEIGHT^(latest - patch)
        (дефолт 0.4 — середина рекомендованного 0.3–0.5), не ниже 0.1.
        latest — максимальный известный патч в датасете; неизвестный
        патч (0) не штрафуется — иначе весь старый датасет разом
        обесценился бы при появлении первой строки с патчем."""
        n = len(self.y)
        if self.patches is None:
            return np.ones(n)
        known = self.patches[self.patches > 0]
        if known.size == 0:
            return np.ones(n)
        latest = int(known.max())
        base = float(os.getenv("PATCH_OLD_WEIGHT", "0.4"))
        w = np.where(self.patches > 0,
                     base ** (latest - self.patches.astype(np.float64)),
                     1.0)
        return np.clip(w, 0.1, 1.0)

    def _tier_mask(self, tier: str) -> np.ndarray:
        if self.tiers is None:
            return np.zeros(len(self.y), dtype=bool)
        return self.tiers == tier

    def _bench_mask(self, seed: int = 42) -> np.ndarray:
        """Про-матчи, отведённые в ЭТАЛОН (остальные идут в обучение).

        Раньше весь про-tier был holdout'ом, и модель не видела про-домен
        вообще — а гейт мерил именно на нём. Замер 2026-07-31: skill 0.58
        на пабликах против 0.27 на про при почти равной неопределённости,
        то есть разрыв Brier давал не приор (смещение всего +0.035), а
        чистый перенос на невиданное распределение. При этом про-строк в
        датасете больше, чем паблик-строк, и все они не участвовали в
        обучении.

        PRO_TRAIN_FRAC (доля про В ОБУЧЕНИЕ, дефолт 0.5) режет выборку
        ПО МАТЧАМ и детерминированно по seed: строки одного матча целиком
        попадают либо в train, либо в эталон, а train и гейт с одним seed
        получают одно и то же разбиение — иначе кандидат мерился бы на
        матчах, которые видел.

        PRO_TRAIN_FRAC=0 возвращает прежнее поведение (весь про — эталон).
        """
        pro = self._tier_mask(PRO_TIER)
        frac = float(os.getenv("PRO_TRAIN_FRAC", "0.5"))
        if not pro.any() or frac <= 0:
            return pro
        matches = sorted(set(self.groups[pro].tolist()))
        # Своя перестановка, независимая от valid-сплита: общий seed на
        # обеих выборках сцепил бы разбиения.
        rng = random.Random(seed + 7919)
        rng.shuffle(matches)
        n_train = min(int(len(matches) * frac), len(matches) - 1)
        to_train = set(matches[:max(n_train, 0)])
        return pro & np.array([g not in to_train for g in self.groups])

    def benchmark(self) -> tuple[np.ndarray, np.ndarray]:
        """Эталонная выборка (про-матчи, отведённые в holdout)."""
        m = self._bench_mask()
        return self.X[m], self.y[m]

    def _valid_mask(self, valid_frac: float = 0.2, seed: int = 42):
        """Маска валидационных строк (group split по НЕэталонным матчам)
        и маска эталона. Детерминирована по seed — один и тот же holdout
        воспроизводится и в train, и в гейте."""
        pro = self._bench_mask(seed)
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
        pro = self._bench_mask(seed)
        pro_matches = sorted(set(self.groups[pro].tolist()))
        if len(pro_matches) >= min_bench_matches:
            return self.X[pro], self.y[pro], self.groups[pro], "benchmark_pro"
        in_valid, _ = self._valid_mask(valid_frac, seed)
        va = in_valid & ~pro
        return self.X[va], self.y[va], self.groups[va], "valid"


# Колонки витрины, из которых row_to_features собирает вектор (draft_prior
# лежит отдельно — в MatchDraft, подтягивается джойном).
#
# Единый источник истины для ЛЮБОГО чтения витрины под инференс. До трека F
# predictors/explain/report-generator держали каждый свой список из 10
# колонок; после расширения FEATURES до 22 такие читатели молча кормили бы
# модель NaN вместо реальных значений, а на 22-фичном артефакте Predict
# падал бы «missing features». Списку положено быть одним.
ROW_COLUMNS = [
    "game_time", "networth_diff", "networth_total", "xp_diff",
    "kills_radiant", "kills_dire", "position_advance", "alive_diff",
    "towers_diff", "rax_diff",
    # A14 (миграция 014): перевес в точке контакта и собранность команды
    "local_manpower_diff", "spread_diff",
    # Трек F (миграция 012)
    "roshan_diff", "aegis_alive", "buybacks_diff", "first_blood",
    "item_value_diff", "key_items_diff", "obs_wards_diff", "sen_wards_diff",
    "runes_diff", "neutral_tier_diff", "levels_diff",
]


def match_rows_sql(extra: tuple[str, ...] = (), where: str = "") -> str:
    """SELECT поминутных строк витрины со ВСЕМИ колонками row_to_features.

    `extra` — дополнительные колонки витрины (например radiant_win, tier),
    `where` — условие внутри подзапроса (например
    "WHERE match_id = {match_id:UInt64}").
    """
    cols = ", ".join(("match_id", *ROW_COLUMNS, *extra))
    return (f"SELECT t.*, d.prior AS draft_prior"
            f"  FROM (SELECT {cols}"
            f"          FROM MatchTimelineFeatures FINAL {where}) AS t"
            f"  LEFT JOIN (SELECT match_id, prior FROM MatchDraft FINAL) AS d"
            f"    USING (match_id)")


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
        _f("local_manpower_diff"),
        _f("spread_diff"),
        _f("roshan_diff"),
        _f("aegis_alive"),
        _f("buybacks_diff"),
        _f("first_blood"),
        _f("item_value_diff"),
        _f("key_items_diff"),
        _f("obs_wards_diff"),
        _f("sen_wards_diff"),
        _f("runes_diff"),
        _f("neutral_tier_diff"),
        _f("levels_diff"),
        _f("draft_prior"),
    ]


def load_from_clickhouse(url: str, database: str, user: str, password: str) -> Dataset:
    """Все матчи из MatchTimelineFeatures (FINAL — свежая версия фич)."""
    resp = requests.post(
        url,
        params={"database": database, "default_format": "JSONEachRow"},
        data=match_rows_sql(extra=("radiant_win", "tier", "patch"))
             + " ORDER BY match_id, game_time",
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
    patches = np.array([int(r.get("patch") or 0) for r in rows],
                       dtype=np.int64)
    return Dataset(X=X, y=y, groups=groups,
                   n_matches=len(set(groups.tolist())), tiers=tiers,
                   patches=patches)


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
            row = [t, nw, xp, kills_r - kills_d, kills_r + kills_d, adv,
                   alive, towers, rax, nw / total]
            # Фичи трека F синтетика не моделирует — NaN, как у реальных
            # матчей, собранных до их появления. Длина строки обязана
            # совпадать с FEATURES, иначе LightGBM падает на несоответствии.
            row += [math.nan] * (len(FEATURES) - len(row))
            Xs.append(row)
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
    patches_a = (a.patches if a.patches is not None
                 else np.zeros(len(a.y), dtype=np.int64))
    patches_b = (b.patches if b.patches is not None
                 else np.zeros(len(b.y), dtype=np.int64))
    return Dataset(
        X=np.vstack([a.X, b.X]),
        y=np.concatenate([a.y, b.y]),
        groups=np.concatenate([a.groups, b.groups]),
        n_matches=a.n_matches + b.n_matches,
        n_synthetic=a.n_synthetic + b.n_synthetic,
        tiers=np.concatenate([tiers_a, tiers_b]),
        patches=np.concatenate([patches_a, patches_b]),
    )


def dataset_hash(ds: Dataset) -> str:
    """Отпечаток датасета для метаданных артефакта (аналог DVC-хэша)."""
    h = hashlib.sha256()
    h.update(ds.X.tobytes())
    h.update(ds.y.tobytes())
    return h.hexdigest()[:16]
