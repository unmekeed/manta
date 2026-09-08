"""Выбрать якорь среди УЖЕ СУЩЕСТВУЮЩИХ версий (спринт 209).

    ./scripts/in-image.sh ml-write -m training.anchor           # только показать
    ./scripts/in-image.sh ml-write -m training.anchor --apply   # и поставить

ЗАЧЕМ, ЕСЛИ ХРАПОВИК УЖЕ ЕСТЬ. Спринт 207 заводил якорь на ПЕРВОЙ
продвинутой версии — то есть на той, что подвернётся следующей. А
подвернётся, по построению, худшая из виденных: планка встала бы ровно
там, куда съехал прод, и храповик начал бы держать деградацию вместо
того, чтобы её остановить. Замер 08.09.2026, пять последних версий по
их собственному про-эталону:

    0.1581 → 0.1572 → 0.1599 → 0.1594 → 0.1617 (production)

Ставить якорь на 0.1617 значило бы узаконить весь сползший путь.

ПОЧЕМУ НЕЛЬЗЯ ПРОСТО ВЗЯТЬ ЛУЧШЕЕ ИЗ ЭТИХ ЧИСЕЛ. Они посчитаны на
РАЗНЫХ ДАННЫХ: у каждой версии свой прогон и свой эталон, который
пересобирается с приходом про-матчей. Число из реестра годится, чтобы
СУЗИТЬ круг кандидатов, и не годится, чтобы вынести приговор, — то же
различие, из-за которого якорем служит артефакт, а не запомненный Brier
(см. ratchet.py).

ПОЭТОМУ ЗДЕСЬ ЧЕСТНОЕ СРАВНЕНИЕ. Все кандидаты скачиваются и считаются
СЕЙЧАС, на ОДНОМ сегодняшнем holdout — том же, по которому гейт
принимает решение. Это дороже (N скачиваний вместо нуля), но делается
один раз и руками.

ЧЕМ ЭТО ЕЩЁ ПОЛЕЗНО ПОТОМ. Тем же способом якорь восстанавливается,
если указатель потерян, и переставляется осознанно — например, после
смены схемы фич, когда старые версии сравнивать уже не с чем.

ГРАНИЦА. Решающий holdout — про-эталон, как и у гейта. Значит, якорь
выбирается по про-домену и о качестве на пабликах не говорит ничего.
Это не упущение этого модуля, а свойство гейта: пункт G2 роадмапа
(«решает один holdout, остальные справочные») остаётся открытым, и
закрывать его надо там, а не здесь — иначе выбор якоря и выбор
production разъехались бы, и планка мерила бы не то, чем судят.
"""
from __future__ import annotations

import argparse
import io
import logging
import os

logger = logging.getLogger("training.anchor")

MODEL_NAME = "win_probability"

# Сколько последних версий рассматривать. Не «все»: в реестре их 26 и
# будет больше, а скачивание каждой стоит времени и памяти. Последние по
# хронологии — те, что обучены на сопоставимом наборе фич; версия
# полугодовой давности сравнивается уже не с тем.
DEFAULT_LIMIT = 8


def candidates(reg, limit: int, keep: set[str]) -> list[str]:
    """Последние `limit` версий плюс те, что надо рассмотреть обязательно.

    `keep` — production и текущий якорь. Выпади production из окна
    последних, сравнение отвечало бы на вопрос «кто лучший среди старых»,
    умалчивая о том, кого мы крутим прямо сейчас.
    """
    versions = reg.list_versions(MODEL_NAME)
    return sorted(set(versions[-limit:]) | {k for k in keep if k in versions})


def evaluate(reg, names: list[str], X, y, groups
             ) -> list[tuple[str, float, object]]:
    """(версия, Brier, предсказания) на сегодняшнем holdout.

    Предсказания возвращаются, а не выбрасываются: по ним считается
    разброс разницы между двумя версиями, а без него Brier'ы — числа без
    масштаба, и «хуже на 0.0024» нельзя отличить от «одно и то же».

    Недоступная версия ПРОПУСКАЕТСЯ с предупреждением, а не роняет
    выбор: одна битая контрольная сумма не повод остаться без якоря
    вовсе.
    """
    import joblib

    from .train_winprob import _brier, predict_calibrated

    out = []
    for v in names:
        try:
            blob, _ = reg.resolve(MODEL_NAME, v)
            art = joblib.load(io.BytesIO(blob))
            p = predict_calibrated(art, X)
            out.append((v, float(_brier(y, p)), p))
        except Exception as exc:  # noqa: BLE001
            logger.warning("версия %s пропущена: %s", v, exc)
    return out


def detectable(y, p_worse, p_better, groups) -> tuple[float, float, float]:
    """(разрыв, σ разрыва, допуск гейта) между двумя версиями.

    ЗАЧЕМ ОТДЕЛЬНО. Храповик отклоняет кандидата, когда Δ > max(порог,
    σ). Значит, разрыв МЕНЬШЕ σ он не заметит — сколько бы его ни
    показывали в таблице. Планка, установленная на неразличимую разницу,
    выглядит работающей и не делает ничего: присутствие ≠ пригодность.

    Число нужно и само по себе: σ, съедающий разрывы между версиями,
    означает, что эталон мал для различий этого размера (пункт G4
    роадмапа), и следующий шаг — растить эталон, а не крутить пороги.
    """
    from .train_winprob import GATE_TOL_FLOOR, _paired_bootstrap_delta

    delta, sigma = _paired_bootstrap_delta(y, p_worse, p_better, groups)
    return delta, sigma, max(GATE_TOL_FLOOR, sigma)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"сколько последних версий рассматривать "
                         f"(по умолчанию {DEFAULT_LIMIT})")
    ap.add_argument("--apply", action="store_true",
                    help="поставить якорь (без флага — только показать)")
    args = ap.parse_args()

    from registry import registry_from_env

    from .dataset import load_from_clickhouse
    from .ratchet import CHAMPION_STAGE, set_champion, stage_version

    reg = registry_from_env()
    ds = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))

    holdouts = ds.eval_holdouts()
    if not holdouts:
        logger.error("нет сопоставимого holdout — выбирать якорь не на чем")
        return 1
    X, y, groups, kind = holdouts[0]
    n_m = len(set(groups.tolist()))
    print(f"holdout: {kind}, {n_m} матчей, {len(y)} строк "
          f"(датасет {ds.n_matches} матчей)")

    prod = stage_version(reg, MODEL_NAME, "production")
    champ = stage_version(reg, MODEL_NAME, CHAMPION_STAGE)
    names = candidates(reg, args.limit, {v for v in (prod, champ) if v})
    scored = evaluate(reg, names, X, y, groups)
    if not scored:
        logger.error("ни одна версия не прочиталась — якорь не выбран")
        return 1

    scored.sort(key=lambda p: p[1])
    best, best_brier, best_p = scored[0]
    print(f"\nВсе на ОДНИХ данных, посчитано сейчас "
          f"(Brier, меньше — лучше):")
    for v, b, _ in scored:
        marks = ""
        if v == prod:
            marks += "  ← PRODUCTION"
        if v == champ:
            marks += "  ← якорь сейчас"
        if v == best:
            marks += "  ← ЛУЧШАЯ"
        print(f"  {v}  {b:.4f}{marks}")

    # Разрыв сам по себе ничего не говорит: храповик отклоняет кандидата
    # при Δ > max(порог, σ), то есть разрыв МЕНЬШЕ σ он не заметит. Планка
    # на неразличимой разнице выглядит работающей и не делает ничего —
    # присутствие ≠ пригодность.
    by_name = {v: (b, p) for v, b, p in scored}
    if prod and prod in by_name and prod != best:
        prod_b, prod_p = by_name[prod]
        gap, sigma, tol = detectable(y, prod_p, best_p, groups)
        print(f"\nproduction хуже лучшей на {gap:+.4f} "
              f"(σ{sigma:.4f}, допуск гейта {tol:.4f})")
        if gap > tol:
            print("  РАЗЛИЧИМО: храповик такой разрыв заметит и отклонит "
                  "кандидата, съехавшего до нынешнего прода.")
        else:
            print("  В ПРЕДЕЛАХ ШУМА: храповик такой разрыв НЕ ЗАМЕТИТ. "
                  "Планка стоит, но не кусается.")
            print(f"  Эталон в {n_m} матчей мал для различий этого "
                  f"размера — это пункт G4 роадмапа, и лечится он ростом "
                  f"эталона, а не правкой порогов.")

    if not args.apply:
        print("\nПоказ без изменений. Поставить: добавить --apply")
        return 0
    if champ == best:
        print(f"\nЯкорь уже стоит на {best} — менять нечего")
        return 0
    ok = set_champion(reg, MODEL_NAME, best)
    print(f"\nЯкорь {'поставлен на' if ok else 'НЕ поставлен:'} {best}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
