"""Пересчёт карт фарма на уже разобранных матчах (спринты 140–141).

    make farm-core-backfill ARGS="--dry-run"
    make farm-core-backfill ARGS="--limit 200"

ЗАЧЕМ. Дважды подряд поменялось то, что лежит в MatchMapCells:

  спринт 140  появился вид farm_core (только позиции 1–3), и у матчей,
              разобранных раньше, его просто нет;
  спринт 141  у ОБОИХ видов фарма поменялось определение. Раньше фармом
              считалось «герой жив и рядом нет врага» — признак
              безопасности вместо измерения фарма. На фонтане он
              срабатывал безотказно, и карты светились там, где не фармят
              никогда. Теперь фарм — это интервалы, в которые у героя рос
              счётчик добиток.

То есть старые строки не «другие», а неверные, и оставить их значило бы
смотреть на карту, где половина ярких пятен — артефакт.

ПОЧЕМУ ЭТО ВООБЩЕ ВОЗМОЖНО. Из трёх источников, нужных для карты фарма,
TTL стоит только на ReplayEvents (14 дней, миграция 007). PositionSnapshots
и EconomyTimeline живут без TTL — специально, см. миграцию 007. Значит
фарм пересчитывается на любую глубину истории, а вот смерти, варды и смоки
на старых матчах пересчитать было бы уже нечем.

ЧТО ОН ПИШЕТ И ЧЕГО НЕ ТРОГАЕТ. Только строки видов farm и farm_core, и
только вставкой. Никаких удалений: соблазн был воспользоваться
_replace_rows раннера, но он сносит ВСЕ строки матча, а death/ward/smoke
на старых матчах пересчитать нечем — их сырьё истекло по TTL. То есть
«обновление» стёрло бы данные, которые уже не восстановить, и заметно это
стало бы через месяцы, когда кто-то откроет карту вардов старого матча.

ГАШЕНИЕ НУЛЁМ. Новое определение не только двигает пятна, но и убирает
целые области: фонтан, базу, дорогу между лагерями. Вставка замещает
клетку по ключу и ничего не знает про ключи, которых в новом расчёте нет,
поэтому старые пятна остались бы навсегда. Для них пишется строка с n = 0
— на чтении она отсеивается (reportgen/heatmaps.py отбрасывает n <= 0), а
инструмент остаётся тем, чем задуман: он только пишет. DELETE в ClickHouse
— мутация, переписывающая куски целиком, и ради карты это несоразмерная
операция.

Повторный прогон безопасен: MatchMapCells — ReplacingMergeTree по полному
ключу с версией computed_at, и та же клетка замещается, а не удваивается.
Очередь при этом самоосушающаяся: пересчитанный матч получает свежий
computed_at и в выборку больше не попадает.

ПОЧЕМУ СЧИТАЕТ ЧЕРЕЗ build_cells, А НЕ СВОЕЙ ФОРМУЛОЙ. Своя формула
разошлась бы с боевой — не сразу и не заметно, а карты бэкфилла тихо
отличались бы от карт свежих матчей. Поэтому вызывается ровно та же
функция, что и в раннере, а из результата берутся нужные виды. Пустые
map_events и fights передаются намеренно: остальные виды нам не нужны, и
не считать их дешевле, чем считать и выбросить.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs"))

from extractor.clickhouse import ClickHouse  # noqa: E402
from extractor.mapcells import build_cells, core_heroes  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("farm-core-backfill")

# Пересчитываются ОБА вида фарма. farm_core появился в спринте 140, а в
# спринте 141 у обоих поменялось определение — фарм стал измеряться ростом
# добиток вместо «жив и рядом нет врага». Старые строки не «другие», а
# неверные: в них светится фонтан.
KINDS = ("farm", "farm_core")


def pending_matches(ch: ClickHouse, limit: int, cutoff: str) -> list[int]:
    """Матчи с позициями, у которых фарм не пересчитан ПОСЛЕ cutoff.

    Очередь самоосушающаяся, и это главное её свойство. Вставка проставляет
    computed_at = now(), поэтому пересчитанный матч в выборку больше не
    попадает, и прогон можно гнать партиями до пустой очереди, ничего не
    отмечая вручную.

    Условия два, потому что случая два: у матчей до спринта 140 строк
    farm_core нет вовсе, у более поздних они есть, но посчитаны по старому
    определению фарма.

    FINAL обязателен: MatchMapCells — ReplacingMergeTree, и без него старая
    версия строки соседствовала бы с новой, а max(computed_at) по
    несмёрженным кускам давал бы то один ответ, то другой.
    """
    rows = ch.select(
        "SELECT DISTINCT p.match_id AS match_id"
        "  FROM PositionSnapshots AS p"
        " WHERE p.match_id NOT IN ("
        "     SELECT match_id FROM MatchMapCells FINAL"
        "      WHERE kind IN {kinds:Array(String)}"
        "      GROUP BY match_id"
        "     HAVING min(computed_at) >= {cutoff:DateTime})"
        " ORDER BY match_id DESC"
        " LIMIT {limit:UInt32}",
        {"kinds": "['" + "','".join(KINDS) + "']",
         "cutoff": cutoff, "limit": limit})
    return [int(r["match_id"]) for r in rows]


def existing_cells(ch: ClickHouse, match_id: int) -> set[tuple]:
    """Клетки фарма, уже лежащие у матча.

    Нужны, чтобы погасить те, которых в новом расчёте не будет. Новое
    определение убирает целые области — фонтан, базу, дорогу между
    лагерями, — и без гашения они остались бы на карте навсегда: вставка
    замещает клетку по ключу, но ничего не знает о ключах, которых в ней
    нет.
    """
    rows = ch.select(
        "SELECT phase, team, kind, gx, gy FROM MatchMapCells FINAL"
        " WHERE match_id = {match_id:UInt64}"
        "   AND kind IN {kinds:Array(String)} AND n > 0",
        {"match_id": match_id,
         "kinds": "['" + "','".join(KINDS) + "']"})
    return {(r["phase"], int(r["team"]), r["kind"], int(r["gx"]), int(r["gy"]))
            for r in rows}


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


def farm_rows(ch: ClickHouse, match_id: int) -> list[dict]:
    """Строки фарма матча — новые плюс гасящие нули для исчезнувших клеток.

    Пустой список означает «считать нечем», и это НЕ то же самое, что
    «фарма не было»: во втором случае вернутся одни нули.
    """
    teams, heroes, hero_team = roster_of(ch, match_id)
    if not hero_team:
        logger.info("  %s: ростера нет, пропуск", match_id)
        return []

    economy = ch.select(
        # ORDER BY не для красоты: определение фарма читает сэмплы
        # попарно, и порядок здесь — часть смысла. Сортировка есть и в
        # FarmClock, но полагаться на неё одну значило бы держать
        # правильность в одном месте, а причину — в другом.
        "SELECT player_id, game_time, lh FROM EconomyTimeline"
        " WHERE match_id = {match_id:UInt64}"
        " ORDER BY player_id, game_time",
        {"match_id": match_id})
    if not economy:
        # Со спринта 141 экономика нужна не только для ранжирования коров,
        # но и для самого определения фарма. Без неё пересчёт вернул бы
        # пустой фарм — и погасил бы всё, что уже посчитано. Молчаливое
        # обнуление карты хуже, чем непересчитанный матч.
        logger.info("  %s: экономики нет, пропуск", match_id)
        return []

    positions = ch.select(
        "SELECT game_time, hero, x, y, is_alive FROM PositionSnapshots"
        " WHERE match_id = {match_id:UInt64} ORDER BY game_time",
        {"match_id": match_id})
    if not positions:
        logger.info("  %s: позиций нет, пропуск", match_id)
        return []

    cores = core_heroes(economy, teams, heroes)
    rows = [r for r in build_cells(positions, [], [], hero_team, cores,
                                   economy=economy, heroes=heroes)
            if r["kind"] in KINDS]
    for r in rows:
        r["match_id"] = match_id

    # Гашение. Новое определение убирает целые области — фонтан, базу,
    # дорогу между лагерями. Вставка замещает клетку по ключу, но про
    # ключи, которых в новом расчёте НЕТ, она ничего не знает, и старые
    # пятна остались бы на карте навсегда.
    #
    # Ноль, а не удаление: строка с n = 0 отсеивается на чтении (см.
    # reportgen/heatmaps.py), а DELETE в ClickHouse — мутация, переписывающая
    # куски целиком. Заодно инструмент остаётся тем, чем задуман: он
    # только пишет.
    fresh = {(r["phase"], r["team"], r["kind"], r["gx"], r["gy"]) for r in rows}
    for phase, team, kind, gx, gy in existing_cells(ch, match_id) - fresh:
        rows.append({"match_id": match_id, "phase": phase, "team": team,
                     "kind": kind, "gx": gx, "gy": gy, "n": 0})
    return rows


def run(ch, limit: int, dry_run: bool, cutoff: str) -> int:
    """Прогон бэкфилла. Клиент передаётся, а не создаётся внутри.

    Ради проверяемости: самое важное свойство инструмента — он НИЧЕГО не
    удаляет, — проверяется только на подставном клиенте, который помнит,
    что ему приказывали. Пока запись жила внутри main() с настоящим
    клиентом, мутация, добавляющая DELETE прямо перед вставкой, не
    роняла ни одного теста.
    """
    matches = pending_matches(ch, limit, cutoff)
    if not matches:
        logger.info("Матчей с непересчитанным фармом не осталось.")
        return 0

    logger.info("К пересчёту: %d матчей%s", len(matches),
                " (СУХОЙ ПРОГОН, записи не будет)" if dry_run else "")
    done = skipped = cells = 0
    for match_id in matches:
        try:
            rows = farm_rows(ch, match_id)
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

    logger.info("Готово: пересчитано %d, пропущено %d, клеток %d",
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
    ap.add_argument("--cutoff", default=None,
                    help="пересчитывать матчи, чей фарм старше этой отметки "
                         "(UTC 'ГГГГ-ММ-ДД ЧЧ:ММ:СС'); по умолчанию — старт "
                         "скрипта, то есть «всё, что не пересчитано сейчас»")
    args = ap.parse_args()

    # Отметка берётся ОДНА на прогон и до первого запроса. Считай её
    # заново на каждый матч — и матч, пересчитанный секунду назад, снова
    # оказался бы «старым» лишь потому, что часы ушли вперёд.
    cutoff = args.cutoff or datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")

    ch = ClickHouse(
        url=os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DB", "manta"),
        user=os.getenv("CLICKHOUSE_USER", "dota"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password"))
    return run(ch, args.limit, args.dry_run, cutoff)


if __name__ == "__main__":
    raise SystemExit(main())
