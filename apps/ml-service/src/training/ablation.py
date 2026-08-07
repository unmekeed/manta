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

ГДЕ ЕСТЬ и ВЕЗДЕ (спринт 132). Δ считается дважды, и это не украшение
отчёта, а исправление ошибки измерения.

Фича, заполненная у 30% строк, физически не может двигать метрику на
остальных 70%: там модель её не видела ни с ней, ни без неё. Измеряя Δ
по всей валидации, мы делим реальный эффект на долю покрытия — эффект
0.003 на своей подвыборке превращается в 0.001 общего и попадает ровно в
порог MIN_EFFECT, то есть в вердикт «практической пользы нет». Инструмент
систематически занижал эффект тем сильнее, чем реже фича, и рекомендовал
бы удалять работающие фичи трека F — те самые, ради ревизии которых он и
писался.

Поэтому вердикт выносится по Δ НА ПОДВЫБОРКЕ, где фича наблюдаема
(«работает ли она там, где она есть»), а Δ по всей валидации печатается
рядом как масштаб продуктового эффекта («сколько это даёт модели
сегодня»). Оговорка, которую надо помнить: подвыборка не случайна —
реплейные матчи отличаются от JSON-матчей, — поэтому первое число это
эффект на популяции матчей с полным сигналом, а не на всём датасете.

ПО ФАЗАМ. Агрегат маскирует фазовую природу: объективы (Рошан, аегис,
бэйбеки) живут в поздней игре, производные трека G — в ранней. Фича,
дающая много в одной фазе и ноль в остальных, в среднем выглядит шумом.
Δ по фазам считается на той же подвыборке.

CLI:
    python -m training.ablation                # группы (быстро, ~11 обучений)
    python -m training.ablation --each         # каждая фича (42 обучения)
    python -m training.ablation --min-coverage 0.05
    python -m training.ablation --json out.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

import numpy as np

from wp_rates import RATE_FEATURES, RATE_METRICS, RATE_WINDOWS, rate_name

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
    # G1 (спринт 131). Четыре группы, а не одна, потому что вопросов два
    # и они разные. «G1_производные_все» отвечает на главный: несут ли
    # производные сигнал вообще. Три семейства окон отвечают на второй:
    # какое окно работает. Скорее всего работает одно из трёх — мерить
    # их скопом значило бы утопить сработавшее в двух несработавших.
    "G1_производные_все": list(RATE_FEATURES),
    **{f"G1_окно_{w // 60}мин": [rate_name(m, w) for m in RATE_METRICS]
       for w in RATE_WINDOWS},
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


# Фазы игры, секунды. Те же границы, что у фазовых Brier в train_winprob:
# сравнивать вклад фичи с фазовой слабостью модели можно только на одной
# нарезке.
PHASES = (("ранняя", 0, 600), ("средняя", 600, 1500),
          ("поздняя", 1500, float("inf")))

# Ниже этого числа строк подвыборка ничего не измеряет: Brier на десятках
# строк — это шум сэмплирования, а не метрика. Бутстрап дополнительно
# требует хотя бы трёх МАТЧЕЙ и сам возвращает σ=0, если их меньше.
MIN_OBSERVED_ROWS = 100


def _valid_predictions(art: dict, ds: Dataset):
    """Предсказания артефакта на нетронутой валидации + её метки/группы/X."""
    from .train_winprob import predict_calibrated

    in_valid, pro = ds._valid_mask()
    m = in_valid & ~pro
    return (predict_calibrated(art, ds.X[m]), ds.y[m], ds.groups[m], ds.X[m])


def observed_mask(X: np.ndarray, names: list[str]) -> np.ndarray:
    """Строки, где группа наблюдаема хотя бы одной своей фичей.

    Именно «хотя бы одной»: группа отключается целиком, поэтому эффект
    возможен везде, где было чему отключаться. Требовать заполненности
    всех фич сразу значило бы сузить подвыборку до пересечения покрытий и
    мерить эффект на другой популяции.
    """
    idx = [FEATURES.index(n) for n in names]
    return ~np.all(np.isnan(X[:, idx]), axis=1)


def _delta_on(mask: np.ndarray, y, p_abl, p_base, groups):
    """(Δ, σ) на подмножестве строк или (None, None), если их мало."""
    from .train_winprob import _paired_bootstrap_delta

    if int(mask.sum()) < MIN_OBSERVED_ROWS:
        return None, None
    d, s = _paired_bootstrap_delta(y[mask], p_abl[mask], p_base[mask],
                                   groups[mask])
    return round(float(d), 5), round(float(s), 5)


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
    from .train_winprob import train

    cov = coverage(ds)
    t0 = time.time()
    base_art = train(ds)
    base_brier = float(base_art["metrics"]["brier_calibrated"])
    logger.info("базовая модель: Brier valid %.4f, обучение %.0fс",
                base_brier, time.time() - t0)
    p_base, y_va, g_va, X_va = _valid_predictions(base_art, ds)
    t_va = X_va[:, FEATURES.index("game_time")]

    rows = []
    for label, names in targets.items():
        names = [n for n in names if n in FEATURES]
        if not names:
            continue
        cov_max = max(cov[n] for n in names)
        # Сколько МАТЧЕЙ вообще несут этот сигнал. Доля строк обманчива:
        # 5% строк могут быть и сотней матчей, и пятью. Ревизия трека F
        # ждала «2–3 тысячи матчей с полными сигналами» — считается это.
        n_matches_obs = len(set(
            ds.groups[observed_mask(ds.X, names)].tolist()))
        if cov_max < min_coverage:
            # Не обучаем: данных нет. Это НЕ вердикт о полезности.
            rows.append({"target": label, "features": names,
                         "coverage": round(cov_max, 4), "delta": None,
                         "sigma": None, "delta_all": None, "observed_rows": 0,
                         "matches_observed": n_matches_obs, "phases": {},
                         "verdict": "НЕИЗМЕРИМА — фичи пусты в датасете"})
            logger.info("%-24s покрытие %.1f%% — пропуск, данных нет",
                        label, cov_max * 100)
            continue

        art = train(without(ds, names))
        p_abl, _, _, _ = _valid_predictions(art, ds)

        # Продуктовый эффект: что фича даёт модели на всей валидации.
        delta_all, _ = _delta_on(np.ones(len(y_va), dtype=bool),
                                 y_va, p_abl, p_base, g_va)
        # Вердикт: работает ли фича ТАМ, ГДЕ ОНА ЕСТЬ. На строках без неё
        # обе модели видели одно и то же, и включать их в знаменатель
        # значит делить эффект на долю покрытия (см. модульный docstring).
        obs = observed_mask(X_va, names)
        delta, sigma = _delta_on(obs, y_va, p_abl, p_base, g_va)

        if delta is None:
            # Покрытие по датасету есть, а на ВАЛИДАЦИИ фичи почти нет:
            # так бывает, когда матчи с полным сигналом осели в train.
            # Это не вердикт о фиче, а отказ его выносить.
            rows.append({"target": label, "features": names,
                         "coverage": round(cov_max, 4), "delta": None,
                         "sigma": None, "delta_all": delta_all,
                         "observed_rows": int(obs.sum()),
                         "matches_observed": n_matches_obs, "phases": {},
                         "verdict": "НЕИЗМЕРИМА — на валидации почти нет строк"})
            logger.info("%-24s покрытие %5.1f%%, но на валидации %d строк — "
                        "вывод невозможен", label, cov_max * 100, obs.sum())
            continue

        phases = {}
        for ph_label, lo, hi in PHASES:
            d, s = _delta_on(obs & (t_va >= lo) & (t_va < hi),
                             y_va, p_abl, p_base, g_va)
            if d is not None:
                phases[ph_label] = {"delta": d, "sigma": s}

        v = verdict(delta, sigma, cov_max)
        rows.append({"target": label, "features": names,
                     "coverage": round(cov_max, 4),
                     "delta": delta, "sigma": sigma,
                     "delta_all": delta_all,
                     "observed_rows": int(obs.sum()),
                     "matches_observed": n_matches_obs, "phases": phases,
                     "verdict": v})
        logger.info("%-24s покрытие %5.1f%%  Δ(где есть)%+.5f ±%.5f  "
                    "Δ(везде)%+.5f  %s",
                    label, cov_max * 100, delta, sigma,
                    delta_all if delta_all is not None else float("nan"), v)
    return rows, base_brier


def _phase_report(rows: list[dict]) -> list[str]:
    """Таблица Δ по фазам игры для групп, где фаза что-то показала.

    Отдельным блоком, а не колонками основной таблицы: фазовая разбивка
    интересна не для всех групп, и в одну строку она не влезает без
    потери читаемости.
    """
    measured = [r for r in rows if r.get("phases")]
    if not measured:
        return []
    names = [p[0] for p in PHASES]
    head = "  ".join(f"{n:>16}" for n in names)
    out = ["", "Δ по фазам игры (0–10 / 10–25 / 25+ мин), там где фича есть.",
           "Звёздочка — эффект превышает 2σ на этой фазе.",
           "", f"{'группа/фича':<24} {head}", "-" * (24 + len(head) + 2)]
    for r in measured:
        cells = []
        for n in names:
            ph = r["phases"].get(n)
            if ph is None:
                cells.append(f"{'—':>16}")
            else:
                mark = "*" if abs(ph["delta"]) > 2 * ph["sigma"] > 0 else " "
                cells.append(f"{ph['delta']:+10.5f}{mark}     ")
        out.append(f"{r['target']:<24} " + "  ".join(cells))
    return out


def _print_report(rows: list[dict], base_brier: float) -> None:
    print()
    print(f"Базовый Brier (валидация): {base_brier:.4f}")
    print("Δ = Brier(без фичи) − Brier(со всеми). Δ > 0 → фича полезна.")
    print()
    print("«где есть» — только строки, в которых фича заполнена: вердикт")
    print("выносится по нему, иначе эффект делится на долю покрытия.")
    print("«везде» — вся валидация: масштаб продуктового эффекта сегодня.")
    print()
    print(f"{'группа/фича':<24} {'покр.':>6} {'Δ где есть':>11} {'σ':>8} "
          f"{'Δ везде':>9}  вердикт")
    print("-" * 110)
    for r in rows:
        d_all = r.get("delta_all")
        all_cell = f"{d_all:+9.5f}" if d_all is not None else f"{'—':>9}"
        if r["delta"] is None:
            print(f"{r['target']:<24} {r['coverage']*100:5.1f}% "
                  f"{'—':>11} {'—':>8} {all_cell}  {r['verdict']}")
        else:
            print(f"{r['target']:<24} {r['coverage']*100:5.1f}% "
                  f"{r['delta']:+11.5f} {r['sigma']:8.5f} {all_cell}  "
                  f"{r['verdict']}")
    for line in _phase_report(rows):
        print(line)
    print()
    def by(mark: str) -> list[str]:
        return [r["target"] for r in rows if mark in r["verdict"]]

    def with_matches(labels: list[str]) -> str:
        """Названия с числом матчей, несущих сигнал.

        Отложенный вердикт без этого числа бесполезен: «вернуться при
        росте данных» не отвечает на вопрос «насколько ещё вырасти».
        """
        by_label = {r["target"]: r.get("matches_observed") for r in rows}
        return ", ".join(
            f"{n} ({by_label[n]} матчей)" if by_label.get(n) is not None else n
            for n in labels)

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
              + with_matches(early))
    if blind:
        print("Неизмеримо (нет данных, вернуться после накопления): "
              + with_matches(blind))
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
