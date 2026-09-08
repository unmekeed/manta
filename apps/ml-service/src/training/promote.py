"""Осознанно поставить в production выбранную версию (спринт 211).

    ./scripts/in-image.sh ml-write -m training.promote 0.9.0-20260907T083343Z
    ./scripts/in-image.sh ml-write -m training.promote <версия> --apply

ЗАЧЕМ. Замер 08.09.2026 показал, что обслуживает запросы НЕ ЛУЧШАЯ из
имеющихся моделей: production 0.1622 против 0.1598 у версии суточной
давности, разрыв 2.4σ на общем holdout из 668 матчей. Откатить было
нечем: реестр умеет `promote`, но наружу это никогда не выходило —
production назначал только гейт, и только вперёд.

Ждать, пока обучение обгонит, нельзя вдвойне. Во-первых, отчёты всё это
время считает заведомо худшая модель. Во-вторых, храповик (спринт 207)
теперь отклоняет всё, что значимо хуже якоря, — то есть застрявший прод
сам себя не вылечит, он именно застрянет.

ПОЧЕМУ ЭТО НЕ ДЫРА В ГЕЙТЕ. Правило то же, что у автоматического
промоушена: значимо хуже якоря — нельзя. Отличие ровно одно — версию
называет человек, а не обучение. `--force` существует (мета сменилась,
якорь пора переставить), но требует сказать это вслух, и отказ без него
называет число, которое придётся перешагнуть.

СНАЧАЛА ПОКАЗ, ПОТОМ ДЕЙСТВИЕ. Без `--apply` команда только считает и
печатает. Промоушен меняет то, чем сервис отвечает пользователю, — на
такое соглашаются, а не набирают по ошибке.
"""
from __future__ import annotations

import argparse
import logging
import os

logger = logging.getLogger("training.promote")

MODEL_NAME = "win_probability"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="версия реестра, которую ставить")
    ap.add_argument("--apply", action="store_true",
                    help="назначить production (без флага — только показать)")
    ap.add_argument("--force", action="store_true",
                    help="поставить, даже если версия значимо хуже якоря")
    args = ap.parse_args()

    from registry import registry_from_env

    from .anchor import detectable, evaluate
    from .dataset import load_from_clickhouse
    from .ratchet import CHAMPION_STAGE, set_champion, stage_version

    reg = registry_from_env()
    if args.version not in reg.list_versions(MODEL_NAME):
        logger.error("версии %s в реестре нет", args.version)
        return 1

    ds = load_from_clickhouse(
        os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        os.getenv("CLICKHOUSE_DB", "manta"),
        os.getenv("CLICKHOUSE_USER", "dota"),
        os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    holdouts = ds.eval_holdouts()
    if not holdouts:
        logger.error("нет сопоставимого holdout — судить не на чем")
        return 1
    X, y, groups, kind = holdouts[0]
    n_m = len(set(groups.tolist()))

    prod = stage_version(reg, MODEL_NAME, "production")
    champ = stage_version(reg, MODEL_NAME, CHAMPION_STAGE)
    # Одна версия — одна строка. Списки пересекаются сплошь и рядом:
    # ставим якорь, откатываемся на production, якорь совпал с продом. С
    # повтором вывод читается как «две разные версии с одинаковым
    # номером» — ровно та беда, что в спринте 198e задваивала источники
    # на странице состояния, и ровно так же выглядит безобидно.
    wanted, seen = [], set()
    for v in (args.version, prod, champ):
        if v and v not in seen:
            seen.add(v)
            wanted.append(v)
    scored = {v: (b, p) for v, b, p in evaluate(reg, wanted, X, y, groups)}
    if args.version not in scored:
        logger.error("версия %s не прочиталась из реестра", args.version)
        return 1

    print(f"holdout: {kind}, {n_m} матчей, {len(y)} строк")
    for v in wanted:
        if v not in scored:
            continue
        marks = "".join(["  ← ставим" if v == args.version else "",
                         "  ← PRODUCTION" if v == prod else "",
                         "  ← якорь" if v == champ else ""])
        print(f"  {v}  {scored[v][0]:.4f}{marks}")

    # Правило то же, что у автоматического промоушена. Своя копия
    # арифметики здесь означала бы, что «значимо хуже» у ручного и
    # автоматического пути считается по-разному, и разъезд проявился бы
    # не падением, а разными вердиктами на одних числах.
    blocked, raises_bar = False, False
    if champ and champ in scored and champ != args.version:
        gap, sigma, tol = detectable(y, scored[args.version][1],
                                     scored[champ][1], groups)
        print(f"\nотносительно якоря: {gap:+.4f} (σ{sigma:.4f}, "
              f"допуск {tol:.4f})")
        blocked = gap > tol
        raises_bar = gap <= -tol

    if not args.apply:
        print("\nПоказ без изменений. Назначить: добавить --apply")
        return 0
    if blocked and not args.force:
        print("\nОТКАЗ: версия значимо хуже якоря. Это то же правило, по "
              "которому гейт отклоняет кандидатов, — ручной путь его не "
              "отменяет.")
        print("Если якорь устарел (сменилась мета, схема фич), это "
              "отдельное решение: --force, а лучше сперва "
              "`-m training.anchor --apply`.")
        return 1

    reg.promote(MODEL_NAME, args.version)
    print(f"\nPRODUCTION: {args.version}"
          f"{'  (через --force, вопреки якорю)' if blocked else ''}")
    # Планка идёт вверх по тому же правилу, что и у гейта: только по
    # доказанному улучшению. Ручной промоушен — не повод её сдвинуть на
    # ничью.
    if raises_bar and set_champion(reg, MODEL_NAME, args.version):
        print(f"ЯКОРЬ: {args.version} (версия лучше прежнего якоря)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
