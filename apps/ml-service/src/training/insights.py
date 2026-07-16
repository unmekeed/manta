"""CLI разбора production-модели: python -m training.insights

«Что модель поняла про Dota»: важности фич LightGBM (gain — вклад в
качество сплитов), направления монотонных ограничений и чувствительность
калиброванной вероятности к каждой фиче (пробы вокруг типичной середины
игры). Ходит только в реестр моделей (MinIO) — безопасно в любой момент.

Аргумент --version <registry_version> — разобрать конкретную версию
вместо production.
"""
from __future__ import annotations

import argparse
import io
import sys

import joblib
import lightgbm as lgb
import numpy as np

from registry import registry_from_env

from .dataset import FEATURES
from .train_winprob import MONOTONE, predict_calibrated

MODEL = "win_probability"

# Человеческое объяснение фич — для вывода, не для модели.
EXPLAIN = {
    "game_time": "минута игры (контекст: одно и то же преимущество весит "
                 "по-разному в раннюю и позднюю игру)",
    "networth_diff": "разница net worth Radiant−Dire (главный сигнал: "
                     "золото → предметы → сила в драках)",
    "xp_diff": "разница опыта (уровни, таланты, ульты)",
    "kills_diff": "разница убийств (моментум и контроль карты)",
    "kills_total": "суммарные убийства (темп игры, не сторона)",
    "position_advance": "территориальное продвижение [-1..1] "
                        "(прокси контроля карты: бой у чужой базы)",
}

# Точка «типичной середины игры» для проб чувствительности.
BASE_POINT = {
    "game_time": 1200.0,       # 20-я минута
    "networth_diff": 0.0,
    "xp_diff": 0.0,
    "kills_diff": 0.0,
    "kills_total": 20.0,
    "position_advance": 0.0,
}

# Амплитуда пробы: сдвиг фичи на «ощутимую» величину.
PROBE_DELTA = {
    "game_time": 600.0,        # ±10 минут
    "networth_diff": 5000.0,   # ±5k золота
    "xp_diff": 5000.0,
    "kills_diff": 5.0,
    "kills_total": 10.0,
    "position_advance": 0.5,
}

ARROW = {1: "↑ монотонно ЗА Radiant", -1: "↓ монотонно ПРОТИВ", 0: "· свободно"}


def _bar(share: float, width: int = 28) -> str:
    n = int(round(share * width))
    return "█" * n + "░" * (width - n)


def sensitivity(art: dict, feature: str) -> float:
    """ΔWP при сдвиге фичи на +PROBE_DELTA от середины игры."""
    base = np.array([[BASE_POINT[f] for f in FEATURES]])
    probe = base.copy()
    probe[0, FEATURES.index(feature)] += PROBE_DELTA[feature]
    lo, hi = predict_calibrated(art, base)[0], predict_calibrated(art, probe)[0]
    return float(hi - lo)


def render(art: dict, meta: dict | None, version: str) -> str:
    booster = lgb.Booster(model_str=art["booster"])
    gains = dict(zip(booster.feature_name(),
                     booster.feature_importance("gain")))
    total = sum(gains.values()) or 1.0

    lines = ["=" * 64,
             "  Manta · Win Probability — что модель поняла про Dota",
             "=" * 64,
             f"версия: {version}   алгоритм: {art.get('algo', '?')}"]
    if meta:
        m = meta.get("metrics", {})
        lines.append(
            f"обучена на {meta.get('dataset', {}).get('matches', '?')} матчах"
            f" · Brier pro {m.get('brier_benchmark_pro', '—')}"
            f" · valid {m.get('brier_calibrated', '—')}")
    lines.append("")
    lines.append("Важность фич (доля gain) и направление:")

    for f in sorted(FEATURES, key=lambda f: -gains.get(f, 0.0)):
        share = gains.get(f, 0.0) / total
        lines.append(f"  {f:<17} {_bar(share)} {share:5.1%}  "
                     f"{ARROW[MONOTONE[f]]}")
        lines.append(f"  {'':<17} {EXPLAIN[f]}")

    lines.append("")
    lines.append("Чувствительность на 20-й минуте (калиброванная ΔWP "
                 "при сдвиге фичи с нуля/базы):")
    for f in FEATURES:
        d = sensitivity(art, f)
        sign = "+" if d >= 0 else ""
        lines.append(f"  {f:<17} +{PROBE_DELTA[f]:>6.0f} → "
                     f"WP Radiant {sign}{d * 100:.1f} п.п.")

    lines.append("")
    lines.append("Ограничения честности: модель видит только командные"
                 " дифференциалы таймлайна — ни героев, ни предметов, ни"
                 " драфта; монотонность по золоту/опыту/убийствам/территории"
                 " зашита как доменное знание (Гл. 6.2.2), поэтому «золото"
                 " решает» — не открытие модели, а рамка, внутри которой она"
                 " выучила ВЕС каждого преимущества по времени игры.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default="production",
                    help="версия в реестре (по умолчанию production)")
    args = ap.parse_args()

    reg = registry_from_env()
    try:
        raw, meta = reg.resolve(MODEL, args.version)
    except KeyError:
        print(f"версия '{args.version}' не найдена в реестре", file=sys.stderr)
        return 1
    art = joblib.load(io.BytesIO(raw))
    version = (meta or {}).get("registry_version", args.version)
    print(render(art, meta, version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
