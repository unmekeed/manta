"""Пересчёт фич по СОХРАНЁННОМУ сырому JSON — без единого вызова API.

Зачем. Квота OpenDota — главный дефицит проекта, и до сих пор каждая
новая JSON-фича начиналась с нуля: колонка появлялась в витрине пустой и
наполнялась только новыми матчами, неделями. Спринт 60 завёл `rawstore`
(сырой JSON матча в MinIO) ровно ради права пересчитать что угодно
задним числом — но читателя у хранилища не было, и право оставалось
неиспользованным.

Что делает. Для каждого сохранённого матча берёт JSON из MinIO,
пересчитывает поминутные фичи трека F, драфт и события — и вписывает их
в уже существующие строки витрины.

Чего НЕ делает и почему это важно:

* **Не трогает колонки, которых не считает.** Строка витрины читается
  целиком, подменяются только колонки трека F, остальное пишется
  как было. Иначе бэкфилл затёр бы `position_advance`/`alive_diff` у
  реплейных матчей — их источник совсем другой, и восстановить их из
  JSON невозможно.
* **Не удаляет события.** MatchEvents — ReplacingMergeTree, повторная
  вставка замещает строку с тем же ключом, но НЕ удаляет строки,
  которых новый расчёт не породил. Бэкфилл умеет добавлять и уточнять,
  не умеет вычищать.
* **Не ходит в сеть за матчами.** Единственные внешние вызовы — S3 и
  ClickHouse. Если матча нет в хранилище, он пропускается: докачивать
  его значило бы тратить ту самую квоту, ради экономии которой всё это
  и затевалось.

Запуск:

    python -m collector.backfill                 # всё хранилище
    python -m collector.backfill --limit 50      # проба на полусотне
    python -m collector.backfill --match-id N    # один матч
    python -m collector.backfill --only-missing  # только пустые колонки
    python -m collector.backfill --dry-run       # ничего не пишет
"""
from __future__ import annotations

import argparse
import logging
import math
import time

import requests

from .rawstore import RawMatchStore
from .signals import all_minute_features, draft_row, event_rows
from .timeline_runner import (DRAFT_COLUMNS, EVENT_COLUMNS, F_TRACK_COLUMNS,
                              MTF_COLUMNS, TimelineConfig)

logger = logging.getLogger("collector.backfill")

GAME_TIME = MTF_COLUMNS.index("game_time")

# По каким колонкам судим «сигналы у матча уже есть».
#
# ПО ВСЕМ, которые бэкфилл умеет считать, а не по одной. Одна проба
# ломается ровно тогда, когда бэкфилл нужнее всего: волна добавляет новые
# колонки, старая проба заполнена — и `--only-missing` пропускает КАЖДЫЙ
# матч, сообщая «уже заполнены». Именно это и случилось 2026-09-03 после
# трёх волн подряд: 2353 матча пропущены, у 2744 драки и состав пустые.
#
# Предупреждение об этом стояло в коде комментарием («новая фича добавляет
# НОВУЮ колонку, мерить надо по ней») и не сработало: комментарий читают,
# а умолчание применяют. Поэтому теперь умолчание само право, а `--probe`
# остался для точечных случаев.
#
# Цена: матч, у которого колонка законно пуста (драк не было), будет
# пересчитываться каждый прогон. При 70 матчах в секунду это десятки
# секунд на весь датасет — дешевле одного пропущенного бэкфилла.
PROBE_COLUMN = "roshan_diff"


def fmt(v) -> str:
    """Значение в TabSeparated. nan пишется словом: JSONEachRow не принял
    бы литерал NaN, а TSV его разбирает штатно (см. timeline_runner)."""
    if isinstance(v, float) and math.isnan(v):
        return "nan"
    if isinstance(v, list):
        return "[" + ",".join("'" + str(x).replace("'", "\\'") + "'"
                              for x in v) + "]"
    return str(v)


class Backfiller:
    def __init__(self, cfg: TimelineConfig, store: RawMatchStore,
                 dry_run: bool = False) -> None:
        self._cfg = cfg
        self._store = store
        self._dry = dry_run
        self.stats = {"матчей": 0, "обновлено": 0, "нет в витрине": 0,
                      "нет в хранилище": 0, "без фич": 0, "уже заполнены": 0,
                      "ошибок": 0}

    # -- ClickHouse -----------------------------------------------------------

    def _ch(self, query: str, data: bytes | None = None) -> str:
        resp = requests.post(
            self._cfg.clickhouse_url,
            params={"database": self._cfg.clickhouse_db, "query": query},
            data=data,
            headers={"X-ClickHouse-User": self._cfg.clickhouse_user,
                     "X-ClickHouse-Key": self._cfg.clickhouse_password},
            timeout=120)
        resp.raise_for_status()
        return resp.text

    def _rows_of(self, match_id: int) -> list[list[str]]:
        """Строки витрины матча как СЫРОЙ TSV.

        Значения намеренно не приводятся к числам: колонки, которые
        бэкфилл не считает, должны вернуться в таблицу байт в байт.
        Round-trip через float молча испортил бы точность, а через
        Decimal — потребовал бы знать тип каждой колонки.
        """
        cols = ", ".join(MTF_COLUMNS)
        text = self._ch(f"SELECT {cols} FROM MatchTimelineFeatures FINAL "
                        f"WHERE match_id = {int(match_id)} "
                        f"ORDER BY game_time FORMAT TabSeparated")
        return [line.split("\t") for line in text.splitlines() if line]

    def _insert(self, table: str, columns: list[str], rows: list) -> None:
        if not rows or self._dry:
            return
        if isinstance(rows[0], dict):
            lines = ["\t".join(fmt(r.get(c, "")) for c in columns)
                     for r in rows]
        else:
            lines = ["\t".join(r) for r in rows]
        self._ch(f"INSERT INTO {table} ({', '.join(columns)}) "
                 f"FORMAT TabSeparated",
                 data=("\n".join(lines) + "\n").encode())

    # -- один матч ------------------------------------------------------------

    def process(self, match_id: int, only_missing: bool = False,
                probe_column: str | None = None) -> bool:
        self.stats["матчей"] += 1
        rows = self._rows_of(match_id)
        if not rows:
            # Матч есть в хранилище, но не в витрине: собран другой
            # машиной кластера либо витрина пережила импорт слепка.
            self.stats["нет в витрине"] += 1
            return False
        if only_missing:
            # probe_column=None — умолчание: смотрим ВСЕ колонки трека,
            # то есть проверяем ЭФФЕКТ («всё, что бэкфилл умеет, на
            # месте»), а не артефакт («одна колонка не пуста»).
            probes = ([MTF_COLUMNS.index(probe_column)] if probe_column
                      else [MTF_COLUMNS.index(c) for c in F_TRACK_COLUMNS])
            if all(r[i].lower() not in ("nan", "-nan", "")
                   for r in rows for i in probes):
                self.stats["уже заполнены"] += 1
                return False

        raw = self._store.get(match_id)
        if not raw:
            self.stats["нет в хранилище"] += 1
            return False

        minutes = [int(r[GAME_TIME]) for r in rows]
        feats = all_minute_features(raw, minutes)
        changed = 0
        for col, values in feats.items():
            if col not in F_TRACK_COLUMNS:
                continue
            if len(values) != len(minutes):
                # Молчаливое усечение по zip() испортило бы хвост матча:
                # часть минут получила бы новые значения, часть осталась
                # со старыми, и различить это в витрине было бы нечем.
                logger.warning(
                    "матч %d: %s даёт %d значений на %d минут — колонка "
                    "пропущена", match_id, col, len(values), len(minutes))
                continue
            ci = MTF_COLUMNS.index(col)
            for r, v in zip(rows, values):
                r[ci] = fmt(v)
            changed += 1
        if not changed:
            self.stats["без фич"] += 1
            return False

        self._insert("MatchTimelineFeatures", MTF_COLUMNS, rows)
        # Драфт и события — из того же JSON. tier берём из строки витрины:
        # в сыром JSON его нет, он проставляется источником при сборе.
        tier_i = MTF_COLUMNS.index("tier")
        patch_i = MTF_COLUMNS.index("patch")
        try:
            d = draft_row(raw)
            if d:
                d["tier"] = rows[0][tier_i]
                d["patch"] = int(rows[0][patch_i])
                self._insert("MatchDraft", DRAFT_COLUMNS, [d])
            evs = event_rows(raw)
            if evs:
                self._insert("MatchEvents", EVENT_COLUMNS, evs)
        except Exception:  # noqa: BLE001 — витрина уже обновлена, она первична
            logger.warning("матч %d: драфт/события не переписаны", match_id,
                           exc_info=True)
        self.stats["обновлено"] += 1
        return True

    def run(self, match_ids, only_missing: bool = False,
            limit: int | None = None,
            probe_column: str | None = None) -> None:
        t0 = time.monotonic()
        for i, mid in enumerate(match_ids, 1):
            if limit and i > limit:
                break
            try:
                self.process(mid, only_missing=only_missing,
                             probe_column=probe_column)
            except Exception:  # noqa: BLE001 — один матч не рушит прогон
                self.stats["ошибок"] += 1
                logger.warning("матч %d: пропущен", mid, exc_info=True)
            if i % 100 == 0:
                logger.info("обработано %d, обновлено %d, %.1f матч/с", i,
                            self.stats["обновлено"],
                            i / max(time.monotonic() - t0, 1e-9))
        logger.info("итог: %s", ", ".join(f"{k} {v}"
                                          for k, v in self.stats.items() if v))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
               '"service":"backfill","msg":"%(message)s"}')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, help="обработать не больше N матчей")
    p.add_argument("--match-id", type=int, help="только этот матч")
    p.add_argument("--only-missing", action="store_true",
                   help="пропускать матчи, где колонка --probe уже заполнена")
    # Умолчание — ВСЕ колонки трека (см. PROBE_COLUMN). Одна колонка
    # задаётся явно, когда вопрос точечный: «заполнить только тем, у кого
    # нет вот этого».
    p.add_argument("--probe", default=None,
                   choices=[c for c in MTF_COLUMNS],
                   help="одна колонка-индикатор вместо всех колонок трека")
    p.add_argument("--dry-run", action="store_true",
                   help="считать, но ничего не писать")
    args = p.parse_args()

    store = RawMatchStore.from_env()
    if store is None:
        raise SystemExit(
            "хранилище сырого JSON недоступно: проверить RAW_MATCH_STORE, "
            "S3_ENDPOINT и что MinIO поднят (make recover)")

    bf = Backfiller(TimelineConfig(), store, dry_run=args.dry_run)
    ids = [args.match_id] if args.match_id else store.iter_match_ids()
    bf.run(ids, only_missing=args.only_missing, limit=args.limit,
           probe_column=args.probe)


if __name__ == "__main__":
    main()
