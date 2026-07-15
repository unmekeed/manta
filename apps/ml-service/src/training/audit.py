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
        print("    (лечится зеркальной аугментацией — train-приор → 0.500)")

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

    print("\nДубликаты match_id между tier:")
    rows = q("SELECT count() FROM (SELECT match_id FROM MatchTimelineFeatures"
             " GROUP BY match_id HAVING countDistinct(tier) > 1)")
    dup = int(rows[0][0]) if rows else 0
    print(f"  {dup}" + ("  ⚠ матч в двух tier сразу" if dup else "  ok"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
