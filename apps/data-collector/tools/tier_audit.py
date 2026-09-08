"""Сверка ярлыка `tier` с наблюдаемым рангом матчей (спринт 199).

    PYTHONPATH=src:../../libs python3 tools/tier_audit.py
    PYTHONPATH=src:../../libs python3 tools/tier_audit.py --days 30

ЗАЧЕМ. У реплейного источника `tier` — КОНСТАНТА КЛАССА, а не свойство
матча: `salts` и `candidates` всегда ставят Pub, `opendota_public` —
Premium. Утверждение удобное и почти всегда верное. Почти: замер
2026-08-04 нашёл среди «Premium» матчи ранга 44 (Legend 4) и 64
(Ancient 4), то есть половина «высокоранговой» выборки высокоранговой не
была.

Цена ошибки здесь модельная, а не датасетная. `tier` делит выборку на
обучение и ПРО-ЭТАЛОН, по которому гейт судит каждую новую версию.
Систематический перекос в разметке означает, что модель учат на одной
популяции, а меряют на другой, — и заметить это по метрикам нельзя, они
все останутся правдоподобными.

ЧТО СЧИТАЕТСЯ. Распределение `avg_rank` внутри каждого `tier`, отдельно
по источникам. Ноль в `avg_rank` означает «ранг неизвестен», а не
«низкий», и потому считается ОТДЕЛЬНОЙ строкой: смешать их значило бы
объявить неизвестное низким и получить ложную тревогу вместо находки.

ЧЕГО ИНСТРУМЕНТ НЕ ДЕЛАЕТ. Ничего не чинит и не переразмечает. Вывод по
итогам — решение человека: планка могла съехать, источник мог начать
отдавать другое, а могло измениться само распределение игроков.
"""
from __future__ import annotations

import argparse
import json
import os

import requests

# Порог «высокого ранга» — тот же, что у фильтра JSON-источников
# (OPENDOTA_MIN_RANK, 80 = Immortal). Берётся отсюда, а не выдумывается:
# вопрос ровно в том, соблюдается ли обещание, которое даёт ярлык.
HIGH_RANK = int(os.getenv("OPENDOTA_MIN_RANK", "80"))

# Уровни, которые ОБЕЩАЮТ высокий ранг. Pub такого не обещает, и низкий
# ранг в нём — норма, а не находка.
HIGH_TIERS = {"Premium"}

SQL = """
SELECT tier,
       count(DISTINCT match_id)                                    AS matches,
       countDistinctIf(match_id, avg_rank = 0)                     AS unknown,
       countDistinctIf(match_id, avg_rank > 0 AND avg_rank < {high:Int16}) AS below,
       quantileExactIf(0.5)(avg_rank, avg_rank > 0)                AS median,
       minIf(avg_rank, avg_rank > 0)                               AS lo,
       maxIf(avg_rank, avg_rank > 0)                               AS hi
  FROM manta.MatchTimelineFeatures
 WHERE computed_at > now() - INTERVAL {days:UInt16} DAY
 GROUP BY tier
 ORDER BY matches DESC
"""


def ch(query: str, params: dict) -> list[dict]:
    resp = requests.post(
        os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123"),
        params={"database": os.getenv("CLICKHOUSE_DB", "manta"),
                "default_format": "JSONEachRow", **params},
        data=query,
        headers={"X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
                 "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                               "dota_dev_password")},
        timeout=120)
    resp.raise_for_status()
    return [json.loads(ln) for ln in resp.text.splitlines() if ln.strip()]


def verdict(tier: str, matches: int, unknown: int, below: int) -> str:
    """Что означает эта строка. Три разных вывода, а не один.

    Неизвестный ранг и низкий ранг — РАЗНЫЕ беды с разным лечением:
    первое чинится тем, чтобы источник начал сообщать ранг, второе —
    планкой фильтра. Слив их в одно число, мы получили бы «что-то не
    так» без указания, что именно.
    """
    known = matches - unknown
    if tier not in HIGH_TIERS:
        return "уровень высокого ранга не обещает — сверять нечего"
    if known == 0:
        return ("РАНГ НЕИЗВЕСТЕН У ВСЕХ — ярлык не сверяется ни с чем; "
                "источник не сообщает ранг")
    share = below / known
    if share >= 0.10:
        return (f"НАРУШЕНИЕ: {below} из {known} матчей с известным рангом "
                f"ниже {HIGH_RANK} ({share:.0%}) — выборка не та, "
                f"за которую себя выдаёт")
    if unknown:
        return (f"в пределах нормы, но у {unknown} матчей ранг неизвестен "
                f"— эта доля не проверена")
    return "ярлык подтверждён наблюдаемым рангом"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14,
                    help="окно замера, суток")
    args = ap.parse_args()

    rows = ch(SQL, {"param_days": str(args.days),
                    "param_high": str(HIGH_RANK)})
    if not rows:
        print("витрина пуста за это окно — сверять нечего")
        return 0

    print(f"Сверка tier с наблюдаемым рангом, окно {args.days} суток "
          f"(порог высокого ранга: {HIGH_RANK})\n")
    hdr = f"{'tier':<14} {'матчей':>7} {'ранг ?':>7} {'ниже':>6} {'медиана':>8}  диапазон"
    print(hdr)
    print("-" * len(hdr))
    problems = 0
    for r in rows:
        tier = str(r["tier"] or "(пусто)")
        matches, unknown = int(r["matches"]), int(r["unknown"])
        below = int(r["below"])
        median = r["median"] or 0
        lo, hi = int(r["lo"] or 0), int(r["hi"] or 0)
        rng = f"{lo}–{hi}" if hi else "—"
        print(f"{tier:<14} {matches:>7} {unknown:>7} {below:>6} "
              f"{float(median):>8.0f}  {rng}")
        v = verdict(tier, matches, unknown, below)
        if v.startswith(("НАРУШЕНИЕ", "РАНГ НЕИЗВЕСТЕН")):
            problems += 1
        print(f"{'':<14} └─ {v}\n")

    if problems:
        print(f"Проблемных уровней: {problems}. Что с этим делать — "
              f"решение человека:\n"
              f"  · неизвестный ранг у реплейных источников до спринта 199 —\n"
              f"    ожидаемо, колонку заполняют только собранные ПОСЛЕ него;\n"
              f"  · низкий ранг при известном — смотреть планку фильтра\n"
              f"    источника и то, что он на самом деле отдаёт.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
