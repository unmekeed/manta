"""Аудит качества обучающего датасета: python -m training.audit

Считает сигналы дрейфа/качества, из-за которых модель может плохо
переноситься на про-эталон: сдвиг приора исхода между train (пабликами)
и benchmark (про), распределение длительности матчей, баланс сторон,
объём выборок. Только SELECT из ClickHouse — безопасно.
"""
from __future__ import annotations

import os

import requests


def _q(url: str, db: str, user: str, pw: str, sql: str) -> list[list[str]]:
    resp = requests.post(url, params={"database": db, "default_format": "TSV"},
                         data=sql, headers={"X-ClickHouse-User": user,
                                            "X-ClickHouse-Key": pw}, timeout=30)
    resp.raise_for_status()
    return [line.split("\t") for line in resp.text.splitlines() if line]


def main() -> int:
    url = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
    db = os.getenv("CLICKHOUSE_DB", "manta")
    user = os.getenv("CLICKHOUSE_USER", "dota")
    pw = os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password")

    def q(sql: str) -> list[list[str]]:
        return _q(url, db, user, pw, sql)

    print("=" * 60)
    print("  Manta · аудит датасета Win Probability")
    print("=" * 60)

    print("\nОбъём и приор исхода по tier (train=Premium, эталон=Professional):")
    rows = q("SELECT tier, count(), round(avg(rw), 3) FROM ("
             "SELECT match_id, tier, any(radiant_win) rw"
             "  FROM MatchTimelineFeatures GROUP BY match_id, tier)"
             " GROUP BY tier ORDER BY tier")
    priors = {}
    for tier, n, wr in rows:
        priors[tier] = float(wr)
        print(f"  {tier:<14} {n:>5} матчей   Radiant WR = {wr}")
    if "Premium" in priors and "Professional" in priors:
        shift = priors["Premium"] - priors["Professional"]
        flag = "⚠ значимый сдвиг приора" if abs(shift) > 0.05 else "ok"
        print(f"  → сдвиг приора train↔эталон: {shift:+.3f}  {flag}")
        # Это приор СЫРОГО датасета. Обучение уже зеркалит строки
        # (train_winprob.train(mirror=True), algo «...+mirror»), поэтому
        # приор, который видит модель, ровно 0.500 — советовать здесь
        # аугментацию значило бы посылать чинить уже починенное. Метрика
        # остаётся полезной как индикатор смещённости выборки.
        print("    (в обучение приор НЕ протекает: строки зеркалируются, "
              "train-приор = 0.500)")
        print("    сдвиг говорит о смещённости выборки пабликов, "
              "а разрыв Brier валидация↔эталон — о разнице доменов паб↔про")

    print("\nДлительность матчей (Premium):")
    rows = q("SELECT countIf(mx<900), countIf(mx BETWEEN 900 AND 2700),"
             "       countIf(mx>2700)"
             "  FROM (SELECT match_id, max(game_time) mx"
             "          FROM MatchTimelineFeatures WHERE tier='Premium'"
             "         GROUP BY match_id)")
    if rows:
        u15, m, o45 = rows[0]
        print(f"  <15 мин: {u15}   15–45 мин: {m}   >45 мин: {o45}")
        if int(u15) > 0:
            print(f"  ⚠ {u15} коротких матчей (ранние сдачи искажают экономику)")

    # Ярлык tier до спринта 94 означал РАЗНОЕ у разных источников:
    # OpenDota отсекал матчи ниже OPENDOTA_MIN_RANK, STRATZ не проверял
    # ранг вовсе. Проявлялось это только косвенно — расхождением Radiant
    # WR внутри одного tier, то есть догадкой. Здесь популяция видна
    # прямо; 0 — ранг неизвестен (строки, собранные до колонки avg_rank).
    print("\nСредний ранг матча по источникам (Premium; 80 = Immortal):")
    rows = q("SELECT feature_version,"
             "       countIf(avg_rank = 0) AS unknown,"
             "       countIf(avg_rank > 0 AND avg_rank < 80) AS below,"
             "       countIf(avg_rank >= 80) AS good,"
             "       round(avgIf(avg_rank, avg_rank > 0), 1) AS mean"
             "  FROM (SELECT DISTINCT match_id, feature_version, avg_rank"
             "          FROM MatchTimelineFeatures FINAL WHERE tier = 'Premium')"
             " GROUP BY feature_version ORDER BY unknown + below + good DESC")
    if not rows:
        print("  (нет данных)")
    for fv, unknown, below, good, mean in rows:
        flag = "  ⚠ ниже порога" if int(below or 0) else ""
        print(f"  {fv:<22} ниже 80: {below:>5}   80+: {good:>5}   "
              f"неизвестен: {unknown:>5}   средний {mean}{flag}")
    print("    avg_rank=0 — матчи, собранные до спринта 94: по ним популяцию")
    print("    не проверить, смотреть надо на приток последних суток")

    print("\nДубликаты match_id между tier:")
    rows = q("SELECT count() FROM (SELECT match_id FROM MatchTimelineFeatures"
             " GROUP BY match_id HAVING countDistinct(tier) > 1)")
    dup = int(rows[0][0]) if rows else 0
    print(f"  {dup}" + ("  ⚠ матч в двух tier сразу" if dup else "  ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
