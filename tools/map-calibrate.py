"""Замер настоящих границ карты по собственным данным (спринт 139).

    make map-calibrate

ЗАЧЕМ. Границы карты в проекте заданы трижды и по-разному (см.
libs/dota_map.py), и одно из значений заведомо неверно: половина стороны
и половина диагонали отличаются в √2 раз, а записаны одним числом 8000.
Взять «правильное» число из интернета — значит поменять один непроверенный
источник на другой. У нас есть свои данные, и они отвечают на вопрос
прямо.

ЧТО ЗАМЕРЯЕТСЯ. Три независимых свидетеля, и сходиться они должны:

  1. Позиции героев (PositionSnapshots) — мировые координаты от парсера.
     Их разброс задаёт фактические пределы проходимой карты.
  2. Варды (MatchEvents, kind='ward_obs') — клеточные координаты
     OpenDota. Их пределы задают, где на самом деле лежат 64..192.
  3. Переводной коэффициент: если пересчитать варды в мировые через
     UNITS_PER_CELL, облака должны СОВПАСТЬ. Расхождение и есть та самая
     рассинхронизация, из-за которой тепловая карта не сойдётся с вардами.

ЧТО СКРИПТ НЕ ДЕЛАЕТ. Ничего не меняет: только читает и печатает. Решение
«поставить такое-то WORLD_HALF» принимает человек, потому что смена
константы означает пересчёт всех тепловых карт, а это решение, а не
следствие замера.

Перцентили, а не минимум с максимумом: одна битая строка с координатой
10^9 сдвинет максимум и утянет за собой всю калибровку. Края карты
надёжнее читать по 0.1 и 99.9 процентилям.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs"))

import dota_map  # noqa: E402

CH_CONTAINER = os.getenv("CH_CONTAINER", "manta-clickhouse-1")
CH_USER = os.getenv("CLICKHOUSE_USER", "dota")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "dota_dev_password")
CH_DB = os.getenv("CLICKHOUSE_DB", "manta")


def ch(query: str) -> str:
    import subprocess
    proc = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--user", CH_USER, "--password", CH_PASS, "--database", CH_DB,
         "-q", query],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"ClickHouse не ответил: {proc.stderr.strip()[:200]}",
              file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout.strip()


def _nums(raw: str) -> list[float]:
    out = []
    for part in raw.replace("\t", " ").split():
        try:
            out.append(float(part))
        except ValueError:
            pass
    return out


def main() -> int:
    print("=" * 62)
    print("  Калибровка координат карты по собственным данным")
    print("=" * 62)
    print(f"\nсейчас в коде: WORLD_HALF={dota_map.WORLD_HALF:.0f}  "
          f"клетки {dota_map.CELL_MIN}..{dota_map.CELL_MAX}  "
          f"шаг клетки {dota_map.UNITS_PER_CELL:.1f}")

    # -- 1. позиции героев: мировые координаты -------------------------------
    print("\n-- позиции героев (PositionSnapshots), мировые единицы")
    rows = ch("""
        SELECT count(),
               quantile(0.001)(x), quantile(0.999)(x),
               quantile(0.001)(y), quantile(0.999)(y),
               min(x), max(x), min(y), max(y)
        FROM PositionSnapshots
    """)
    v = _nums(rows)
    if not v or v[0] == 0:
        print("   таблица пуста — реплеев ещё нет, замерить нечем")
        pos_half = None
    else:
        n, x_lo, x_hi, y_lo, y_hi, xmn, xmx, ymn, ymx = v[:9]
        pos_half = max(abs(x_lo), abs(x_hi), abs(y_lo), abs(y_hi))
        print(f"   строк {int(n)}")
        print(f"   x: {x_lo:9.0f} … {x_hi:9.0f}   (сырые {xmn:.0f} … {xmx:.0f})")
        print(f"   y: {y_lo:9.0f} … {y_hi:9.0f}   (сырые {ymn:.0f} … {ymx:.0f})")
        print(f"   => половина стороны по позициям ≈ {pos_half:.0f}")

    # -- 2. варды: клетки OpenDota -------------------------------------------
    print("\n-- варды (MatchEvents ward_obs), клетки OpenDota")
    rows = ch("""
        SELECT count(),
               quantile(0.001)(x), quantile(0.999)(x),
               quantile(0.001)(y), quantile(0.999)(y)
        FROM MatchEvents
        WHERE kind IN ('ward_obs', 'ward_sen') AND isFinite(x) AND isFinite(y)
    """)
    v = _nums(rows)
    if not v or v[0] == 0:
        print("   вардов с координатами нет")
        cell_lo = cell_hi = None
    else:
        n, cx_lo, cx_hi, cy_lo, cy_hi = v[:5]
        cell_lo = min(cx_lo, cy_lo)
        cell_hi = max(cx_hi, cy_hi)
        print(f"   строк {int(n)}")
        print(f"   x: {cx_lo:7.1f} … {cx_hi:7.1f}")
        print(f"   y: {cy_lo:7.1f} … {cy_hi:7.1f}")
        print(f"   => клетки укладываются в {cell_lo:.1f} … {cell_hi:.1f}"
              f"   (в коде {dota_map.CELL_MIN}..{dota_map.CELL_MAX})")

    # -- 3. сходятся ли два свидетеля ----------------------------------------
    print("\n-- сходятся ли позиции и варды")
    if pos_half is None or cell_lo is None:
        print("   не с чем сравнивать: нужны и позиции, и варды")
    else:
        # Во что превращается наблюдаемый предел клеток по текущему курсу.
        implied = max(abs(dota_map.cell_to_world(cell_lo)),
                      abs(dota_map.cell_to_world(cell_hi)))
        print(f"   предел вардов в мировых по текущему курсу: {implied:.0f}")
        print(f"   предел позиций:                            {pos_half:.0f}")
        diff = abs(implied - pos_half) / max(pos_half, 1.0) * 100
        print(f"   расхождение: {diff:.1f}%")
        if diff < 2:
            print("   => системы согласованы, курс можно оставить")
        else:
            print("   => РАСХОЖДЕНИЕ: на общей подложке варды и тепловая")
            print("      карта разъедутся ровно на эту долю. Нужно свести:")
            print(f"      WORLD_HALF ≈ {pos_half:.0f} даёт шаг клетки "
                  f"{2 * pos_half / (cell_hi - cell_lo):.1f}")

    print("\nМеняя WORLD_HALF, помни: все тепловые карты придётся пересчитать")
    print("(feature-extractor перестроит MatchMapCells при переразборе).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
