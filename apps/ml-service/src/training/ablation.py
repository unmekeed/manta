"""Ablation-анализ фич Win Probability: какая фича заслужила своё место.

Зачем. ML-PLAN §6 требует: «фича, не двинувшая метрику на живом датасете,
— выкидывается». Прецедент был (спринт 25), но измерять это было нечем:
важность LightGBM показывает, что модель ИСПОЛЬЗУЕТ фичу, а не что фича
УЛУЧШАЕТ прогноз. После трека F фич стало 22, и вопрос «какие из них
работают» перестал быть риторическим.

Метод. Для каждой фичи (или группы) она обнуляется в NaN и модель
переобучается ПОЛНОСТЬЮ по тому же протоколу, что в train_winprob:
group-split по матчам с тем же seed, K-fold OOF, зеркалирование,
даунвейт патча. NaN — не «нулевое значение», а нативный пропуск
LightGBM: сплит по такой колонке невозможен, фича действительно
отключена. Валидация та же самая, поэтому сравнение честное и парное.

Значимость. Разница Brier сама по себе не значит ничего: на нашем объёме
данных шум порядка 0.003. Поэтому используется тот же парный бутстрап ПО
МАТЧАМ, что и в гейте продвижения (`_paired_bootstrap_delta`), и вердикт
выносится по отношению Δ к σ, а не по знаку Δ.

Читать вердикт так: Δ = Brier(без фичи) − Brier(со всеми). Больше нуля —
без фичи стало ХУЖЕ, значит фича полезна.

Отдельно и до всяких обучений считается ПОКРЫТИЕ: доля строк, где фича не
NaN. Фича, которой нет в данных, не «бесполезна» — она неизмерима, и
путать эти два вердикта нельзя. Ровно этот случай сейчас у всех 12 фич
трека F: они извлекаются при сборе, поэтому у исторических матчей пусты.

CLI:
    python -m training.ablation                # группы (быстро, ~7 обучений)
    python -m training.ablation --each         # каждая фича (22 обучения)
    python -m training.ablation --min-coverage 0.05
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

import numpy as np

from .dataset import FEATURES, Dataset, load_from_clickhouse

logger = logging.getLogger("ablation")

# Группы трека F: фичи вводились блоками, и выкидывать их осмысленно тоже
# блоками — одиночный ward-счётчик мало что значит без своей пары.
GROUPS: dict[str, list[str]] = {
    "F2_объективы": ["roshan_diff", "aegis_alive", "buybacks_diff",
                     "first_blood"],
    "F4_предметы": ["item_value_diff", "key_items_diff"],
    "F5_вижн_руны": ["obs_wards_diff", "sen_wards_diff", "runes_diff"],
    "F6_нейтралки_уровни": ["neutral_tier_diff", "levels_diff"],
    "F3_драфт_прайор": ["draft_prior"],
    "база_экономика": ["networth_diff", "xp_diff", "networth_rel"],
    "база_объекты": ["towers_diff", "rax_diff", "alive_diff"],
}

# Порог «фича неизмерима»: меньше этой доли непропущенных значений — данных
# просто нет, обучать и делать вывод бессмысленно.
MIN_COVERAGE = 0.02

# Ниже этого покрытия вывод «бесполезна» делать НЕЛЬЗЯ, даже если эффект в
# пределах шума. Найдено на живом прогоне: у alive/towers/rax покрытие 5.5%
# (они есть только у реплейных матчей, JSON-путь их не даёт), и ablation
# честно показал «эффекта нет» — но это утверждение про 5% датасета, а не
# про фичу. Выкинуть их по такому основанию было бы ошибкой.
LOW_COVERAGE = 0.25

# Практическая значимость. Парный бутстрап сравнивает две модели, обученные
# на ОДНИХ данных с одним seed и отличающиеся одной фичей: предсказания почти
# идентичны, поэтому дисперсия РАЗНИЦЫ крошечная (σ ~0.0001) и статистически
# значимым становится любой микроэффект. Это не ошибка бутстрапа — это
# правильная статистика для парного сравнения, но её нельзя читать как
# «фича важна». Порог отсекает эффекты, ничего не значащие на практике:
# 0.001 Brier — треть типичного шага между версиями модели.
MIN_EFFECT = 0.001


def coverage(ds: Dataset) -> dict[str, float]:
    """Доля строк, где фича заполнена (не NaN)."""
    return {name: float(np.mean(~np.isnan(ds.X[:, i])))
            for i, name in enumerate(FEATURES)}


def without(ds: Dataset, names: list[str]) -> Dataset:
    """Копия датасета с обнулёнными в NaN колонками.

    Именно NaN, а не 0: ноль — это осмысленное значение разностной фичи
    («поровну»), модель на нём училась бы ложному сигналу. NaN LightGBM
    трактует как пропуск, сплит по полностью пропущенной колонке
    невозможен — фича отключена по-настоящему.
    """
    idx = [FEATURES.index(n) for n in names]
    X = ds.X.copy()
    X[:, idx] = np.nan
    return Dataset(X=X, y=ds.y, groups=ds.groups, n_matches=ds.n_matches,
                   n_synthetic=ds.n_synthetic, tiers=ds.tiers,
                   patches=ds.patches)


def _valid_predictions(art: dict, ds: Dataset):
    """Предсказания артефакта на нетронутой валидации + её метки/группы."""
    from .train_winprob import predict_calibrated

    in_valid, pro = ds._valid_mask()
    m = in_valid & ~pro
    return (predict_calibrated(art, ds.X[m]), ds.y[m], ds.groups[m])


def verdict(delta: float, sigma: float, cov: float = 1.0) -> str:
    """Вердикт по ДВУМ порогам сразу: статистическому и практическому.

    Одного отношения Δ/σ мало (см. MIN_EFFECT): в парном сравнении почти
    любой микроэффект статистически значим. Одного размера эффекта тоже
    мало: без σ не отличить сигнал от дрожания метрики. Плюс покрытие —
    оно решает, можно ли вообще делать вывод «убрать».
    """
    if sigma <= 0:
        return "мало матчей для оценки"
    significant = abs(delta) > 2 * sigma
    material = abs(delta) >= MIN_EFFECT

    if significant and material:
        return ("ПОЛЕЗНА (без неё заметно хуже)" if delta > 0 else
                "ВРЕДИТ (без неё лучше) — удалить")
    if significant and not material:
        # Эффект различим, но ничтожен: держать фичу можно (она бесплатна),
        # но записывать её в достижения нельзя.
        return f"эффект ничтожен (<{MIN_EFFECT}) — практической пользы нет"
    if cov < LOW_COVERAGE:
        return f"эффекта нет, но покрытие {cov*100:.0f}% — вывод преждевременен"
    return "шум (не отличима от нуля) — кандидат на удаление"


def run(ds: Dataset, targets: dict[str, list[str]],
        min_coverage: float = MIN_COVERAGE) -> tuple[list[dict], float]:
    """Прогнать ablation по группам/фичам.

    Возвращает (строки отчёта, базовый Brier). Базовая модель обучается
    ровно один раз — она же эталон для всех парных сравнений.
    """
    from .train_winprob import _paired_bootstrap_delta, train

    cov = coverage(ds)
    t0 = time.time()
    base_art = train(ds)
    base_brier = float(base_art["metrics"]["brier_calibrated"])
    logger.info("базовая модель: Brier valid %.4f, обучение %.0fс",
                base_brier, time.time() - t0)
    p_base, y_va, g_va = _valid_predictions(base_art, ds)

    rows = []
    for label, names in targets.items():
        names = [n for n in names if n in FEATURES]
        if not names:
            continue
        cov_max = max(cov[n] for n in names)
        if cov_max < min_coverage:
            # Не обучаем: данных нет. Это НЕ вердикт о полезности.
            rows.append({"target": label, "features": names,
                         "coverage": round(cov_max, 4), "delta": None,
                         "sigma": None,
                         "verdict": "НЕИЗМЕРИМА — фичи пусты в датасете"})
            logger.info("%-24s покрытие %.1f%% — пропуск, данных нет",
                        label, cov_max * 100)
            continue

        art = train(without(ds, names))
        p_abl, _, _ = _valid_predictions(art, ds)
        delta, sigma = _paired_bootstrap_delta(y_va, p_abl, p_base, g_va)
        v = verdict(delta, sigma, cov_max)
        rows.append({"target": label, "features": names,
                     "coverage": round(cov_max, 4),
                     "delta": round(delta, 5), "sigma": round(sigma, 5),
                     "verdict": v})
        logger.info("%-24s покрытие %5.1f%%  Δ%+.5f ±%.5f  %s",
                    label, cov_max * 100, delta, sigma, v)
    return rows, base_brier


def _print_report(rows: list[dict], base_brier: float) -> None:
    print()
    print(f"Базовый Brier (валидация): {base_brier:.4f}")
    print("Δ = Brier(без фичи) − Brier(со всеми). Δ > 0 → фича полезна.")
    print()
    print(f"{'группа/фича':<24} {'покр.':>6} {'Δ Brier':>10} {'σ':>8}  вердикт")
    print("-" * 96)
    for r in rows:
        if r["delta"] is None:
            print(f"{r['target']:<24} {r['coverage']*100:5.1f}% "
                  f"{'—':>10} {'—':>8}  {r['verdict']}")
        else:
            print(f"{r['target']:<24} {r['coverage']*100:5.1f}% "
                  f"{r['delta']:+10.5f} {r['sigma']:8.5f}  {r['verdict']}")
    print()
    def by(mark: str) -> list[str]:
        return [r["target"] for r in rows if mark in r["verdict"]]

    dead = by("кандидат на удаление") + by("— удалить")
    blind = [r["target"] for r in rows if r["delta"] is None]
    early = by("вывод преждевременен")
    tiny = by("эффект ничтожен")
    if dead:
        print("Кандидаты на удаление (правило ML-PLAN §6): " + ", ".join(dead))
    if tiny:
        print("Работают, но пренебрежимо (в достижения не записывать): "
              + ", ".join(tiny))
    if early:
        print("Мало покрытия — НЕ удалять, вернуться при росте данных: "
              + ", ".join(early))
    if blind:
        print("Неизмеримо (нет данных, вернуться после накопления): "
              + ", ".join(blind))
    if not (dead or blind or early or tiny):
        print("Все проверенные фичи оправдывают своё место.")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--each", action="store_true",
                    help="каждая фича отдельно (22 обучения) вместо групп")
    ap.add_argument("--min-coverage", type=float, default=MIN_COVERAGE)
    ap.add_argument("--json", help="записать отчёт в файл")
    args = ap.parse_args()

    ds = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    logger.info("датасет: %d матчей, %d строк", ds.n_matches, len(ds.y))

    targets = ({name: [name] for name in FEATURES} if args.each
               else dict(GROUPS))
    # game_time и kills_total не ablate-им: первая задаёт фазу игры (без
    # неё модель теряет смысл), вторая симметрична и служит нормировкой.
    for skip in ("game_time",):
        targets.pop(skip, None)

    rows, base = run(ds, targets, args.min_coverage)
    _print_report(rows, base)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"base_brier": base, "rows": rows}, fh,
                      ensure_ascii=False, indent=1)
        logger.info("отчёт записан в %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
