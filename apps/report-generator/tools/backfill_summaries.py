"""Карточки для отчётов, сгенерированных до спринта 192.

    PYTHONPATH=src:../../libs python3 tools/backfill_summaries.py --dry-run
    PYTHONPATH=src:../../libs python3 tools/backfill_summaries.py

ЗАЧЕМ. `MatchSummaries` заполняется в момент генерации отчёта, а отчётов
к появлению таблицы накопилось больше двух тысяч. Без бэкфилла список
матчей на сайте показывал бы только сгенерированное ПОСЛЕ деплоя — то
есть выглядел бы почти пустым при полной базе.

ОТКУДА ДАННЫЕ — ИЗ CLICKHOUSE, А НЕ ИЗ РАЗБОРА. Первая версия этого
скрипта собирала карточку из `MatchReports.analysis`, и это была ошибка,
которую стоит здесь записать: в схеме MatchAnalysis НЕТ ни победителя,
ни стороны игрока. `analysis.get("winner")` возвращал бы пустую строку, а
`player["team"]` — ноль, поэтому каждый матч получил бы «победил Dire» и
весь состав в одной команде. Ни одна проверка бы не упала: карточки
выглядели бы совершенно нормально.

Витрина же несёт всё: исход, счёт, патч, уровень, длительность и героев.
Ходить в неё из ШЛЮЗА нельзя (путь чтения обязан быть дешёвым), но
разовый инструмент бэкфилла — не путь чтения.

ЧЕГО МАТЧУ МОЖЕТ НЕ ХВАТАТЬ. Витрину чистит ретеншен, и у самых старых
отчётов строк может уже не быть. Такой матч ПРОПУСКАЕТСЯ со счётчиком, а
не записывается наполовину: карточка с нулевым счётом и пустым составом
неотличима от честного матча без убийств, и на сайте она выглядела бы не
как пробел в данных, а как странный матч.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reportgen.summary import UPSERT_SQL, build_summary  # noqa: E402

READ_SQL = """
SELECT r.match_id, r.analysis
  FROM MatchReports r
  LEFT JOIN MatchSummaries s ON s.match_id = r.match_id
 WHERE %(all)s OR s.match_id IS NULL
 ORDER BY r.match_id DESC
 LIMIT %(limit)s
"""

# Последняя строка таймлайна несёт исход, счёт, патч и уровень.
LAST_ROW_SQL = """
SELECT game_time, radiant_win, kills_radiant, kills_dire, patch, tier
  FROM manta.MatchTimelineFeatures FINAL
 WHERE match_id = {match_id:UInt64}
 ORDER BY game_time DESC LIMIT 1
"""

PLAYERS_SQL = """
SELECT team, hero, duration_s
  FROM manta.PlayerMatchFeatures FINAL
 WHERE match_id = {match_id:UInt64} ORDER BY player_id
"""


def ch_select(query: str, match_id: int) -> list[dict]:
    resp = requests.post(
        os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123"),
        params={"database": os.getenv("CLICKHOUSE_DB", "manta"),
                "default_format": "JSONEachRow",
                "param_match_id": str(match_id)},
        data=query,
        headers={"X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "dota"),
                 "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD",
                                               "dota_dev_password")},
        timeout=30)
    resp.raise_for_status()
    return [json.loads(line)
            for line in resp.text.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--all", action="store_true",
                    help="переписать и уже существующие карточки")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dsn = os.getenv("POSTGRES_DSN",
                    "postgresql://dota:dota_dev_password@127.0.0.1:5432/manta")
    written = skipped = 0
    with psycopg.connect(dsn, autocommit=True) as db:
        todo = db.execute(READ_SQL, {"limit": args.limit,
                                     "all": args.all}).fetchall()
        print(f"отчётов к обработке: {len(todo)}")
        for match_id, analysis in todo:
            rows = ch_select(LAST_ROW_SQL, match_id)
            players = ch_select(PLAYERS_SQL, match_id)
            if not rows or not players:
                skipped += 1
                continue
            card = build_summary(match_id, rows, players, analysis)
            if args.dry_run:
                if written < 3:
                    print("  пример:", card)
            else:
                db.execute(UPSERT_SQL, card)
            written += 1
    print(("проверено" if args.dry_run else "записано") + f": {written}")
    print(f"пропущено (витрины уже нет): {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
