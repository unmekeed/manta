"""Досчёт вида farm_core на уже разобранных матчах (спринт 140).

    make farm-core-backfill ARGS="--dry-run"
    make farm-core-backfill ARGS="--limit 200"

ЗАЧЕМ. `farm_core` появился в спринте 140 и пишется при разборе реплея. У
матчей, разобранных раньше, его нет, и кнопка «Фарм коров» на них серая —
то есть новая фича не видна ровно на том, что уже собрано.

ПОЧЕМУ ЭТО ВООБЩЕ ВОЗМОЖНО. Из трёх источников, нужных для карты фарма,
TTL стоит только на ReplayEvents (14 дней, миграция 007). PositionSnapshots
и EconomyTimeline живут без TTL — специально, см. миграцию 007. Значит
фарм пересчитывается на любую глубину истории, а вот смерти, варды и смоки
на старых матчах пересчитать было бы уже нечем.

ЧТО ОН ПИШЕТ И ЧЕГО НЕ ТРОГАЕТ. Только строки kind='farm_core'. Никаких
удалений: соблазн был воспользоваться _replace_rows раннера, но он сносит
ВСЕ строки матча, а death/ward/smoke на старых матчах пересчитать нечем —
их сырьё истекло по TTL. То есть «обновление» стёрло бы данные, которые
уже не восстановить, и заметно это стало бы через месяцы, когда кто-то
откроет карту вардов старого матча.

Повторный прогон безопасен: MatchMapCells — ReplacingMergeTree по полному
ключу с версией computed_at, и та же клетка замещается, а не удваивается.

ПОЧЕМУ СЧИТАЕТ ЧЕРЕЗ build_cells, А НЕ СВОЕЙ ФОРМУЛОЙ. Своя формула
разошлась бы с боевой — не сразу и не заметно, а карты бэкфилла тихо
отличались бы от карт свежих матчей. Поэтому вызывается ровно та же
функция, что и в раннере, а из результата берётся один вид. Пустые
map_events и fights передаются намеренно: остальные виды нам не нужны, и
не считать их дешевле, чем считать и выбросить.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs"))

from extractor.clickhouse import ClickHouse  # noqa: E402
from extractor.mapcells import build_cells, core_heroes  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("farm-core-backfill")

KIND = "farm_core"


def pending_matches(ch: ClickHouse, limit: int) -> list[int]:
    """Матчи с позициями, но без farm_core.

    FINAL обязателен: MatchMapCells — ReplacingMergeTree, и без него уже
    досчитанный матч мог бы попасть в выборку повторно из-за несмёрженных
    кусков. Прогон от этого не испортился бы (вставка идемпотентна), но
    счётчики врали бы, а по ним и судят, закончена ли работа.
    """
    rows = ch.select(
        "SELECT DISTINCT p.match_id AS match_id"
        "  FROM PositionSnapshots AS p"
        " WHERE p.match_id NOT IN ("
        "     SELECT match_id FROM MatchMapCells FINAL"
        "      WHERE kind = {kind:String})"
        " ORDER BY match_id DESC"
        " LIMIT {limit:UInt32}",
        {"kind": KIND, "limit": limit})
    return [int(r["match_id"]) for r in rows]


def roster_of(ch: ClickHouse, match_id: int) -> tuple[dict, dict, dict]:
    """(teams, heroes, hero_team) из витрины фич игроков.

    Ростер берётся оттуда, а не из payload разбора: payload живёт в Kafka
    и до старых матчей уже не достать, а PlayerMatchFeatures — витрина
    без TTL, она есть у всего, что вообще обрабатывалось.
    """
    rows = ch.select(
        "SELECT player_id, team, hero FROM PlayerMatchFeatures FINAL"
        " WHERE match_id = {match_id:UInt64}",
        {"match_id": match_id})
    teams, heroes, hero_team = {}, {}, {}
    for r in rows:
        pid, team, hero = int(r["player_id"]), int(r["team"]), r["hero"] or ""
        teams[pid] = team
        heroes[pid] = hero
        if hero:
            hero_team[hero] = team
    return teams, heroes, hero_team


def farm_core_rows(ch: ClickHouse, match_id: int) -> list[dict]:
    """Строки farm_core матча — или пустой список, если считать нечем."""
    teams, heroes, hero_team = roster_of(ch, match_id)
    if not hero_team:
        logger.info("  %s: ростера нет, пропуск", match_id)
        return []

    economy = ch.select(
        "SELECT player_id, game_time, lh FROM EconomyTimeline"
        " WHERE match_id = {match_id:UInt64}",
        {"match_id": match_id})
    cores = core_heroes(economy, teams, heroes)
    if not cores:
        # Без коров вид не пишется вовсе — та же логика, что в раннере:
        # посчитанный по всем пятерым, он был бы неотличим от честного.
        logger.info("  %s: коров определить не удалось, пропуск", match_id)
        return []

    positions = ch.select(
        "SELECT game_time, hero, x, y, is_alive FROM PositionSnapshots"
        " WHERE match_id = {match_id:UInt64} ORDER BY game_time",
        {"match_id": match_id})
    if not positions:
        logger.info("  %s: позиций нет, пропуск", match_id)
        return []

    rows = [r for r in build_cells(positions, [], [], hero_team, cores)
            if r["kind"] == KIND]
    for r in rows:
        r["match_id"] = match_id
    return rows


def run(ch, limit: int, dry_run: bool) -> int:
    """Прогон бэкфилла. Клиент передаётся, а не создаётся внутри.

    Ради проверяемости: самое важное свойство инструмента — он НИЧЕГО не
    удаляет, — проверяется только на подставном клиенте, который помнит,
    что ему приказывали. Пока запись жила внутри main() с настоящим
    клиентом, мутация, добавляющая DELETE прямо перед вставкой, не
    роняла ни одного теста.
    """
    matches = pending_matches(ch, limit)
    if not matches:
        logger.info("Матчей без farm_core не осталось.")
        return 0

    logger.info("К досчёту: %d матчей%s", len(matches),
                " (СУХОЙ ПРОГОН, записи не будет)" if dry_run else "")
    done = skipped = cells = 0
    for match_id in matches:
        try:
            rows = farm_core_rows(ch, match_id)
        except Exception:  # noqa: BLE001
            # Один битый матч не должен останавливать прогон: их тысячи, а
            # причина сбоя у каждого своя.
            logger.warning("  %s: сбой, пропуск", match_id, exc_info=True)
            skipped += 1
            continue
        if not rows:
            skipped += 1
            continue
        if not dry_run:
            ch.insert_rows("MatchMapCells", rows)
        done += 1
        cells += len(rows)
        logger.info("  %s: %d клеток", match_id, len(rows))

    logger.info("Готово: досчитано %d, пропущено %d, клеток %d",
                done, skipped, cells)
    # Ненулевой код, если не досчитано НИЧЕГО при непустой очереди: молчащий
    # успех на полностью провалившемся прогоне — худший вид отчёта.
    return 0 if done else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100,
                    help="сколько матчей обработать за прогон")
    ap.add_argument("--dry-run", action="store_true",
                    help="посчитать и показать, но НИЧЕГО не писать")
    args = ap.parse_args()

    ch = ClickHouse(
        url=os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DB", "manta"),
        user=os.getenv("CLICKHOUSE_USER", "dota"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    return run(ch, args.limit, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
