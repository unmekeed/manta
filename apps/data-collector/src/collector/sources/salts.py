"""Источник «есть соль — качаем» (спринт 179).

ЗАЧЕМ. Спринт 171 научил проект добывать соли у Game Coordinator, спринт
172 — читать их. Но читал их только CandidateSource, то есть работали они
лишь для матчей из `ReplayCandidates`. На VPS эта очередь пуста: её
наполняет `ranks scan`, которого нет ни в одном расписании (спринт 154).

Живая проверка 2026-09-02 это и показала: солей 16, кандидатов 0. Добыча
шла исправно, а потребителя у добытого не было — я подключил соли к
очереди и не проверил, что очередь на этой машине пустая. Проверил
артефакт («код читает соли»), а не эффект («соли доходят до
скачивания»).

ЧТО ДЕЛАЕТ. Идёт ОТ СОЛЕЙ, а не от очереди: раз соль добыта, а реплея
нет — качаем немедленно. Ни OpenDota, ни STEAM_API_KEY, ни квоты здесь
не участвуют вовсе; всё, что нужно, уже лежит в базе.

    COLLECTOR_SOURCE=salts python -m collector --once

Форма та же, что у ParkedSource, и по той же причине: весь конвейер —
скачивание, S3, Kafka, отметка в CollectedMatches — уже написан в
Collector.collect_once, и ему безразлично, откуда пришла ссылка.
Источнику остаётся отдать MatchRef.

Курсора у него нет и быть не может: курсор описывает движение вперёд по
ленте, а здесь лента — список того, что уже добыто и ещё не скачано, из
которого исполненное исчезает само (по CollectedMatches.has_replay).
"""
from __future__ import annotations

import logging
from typing import Iterable

from . import MatchRef, Shard
from .opendota import OpenDotaSource

logger = logging.getLogger("collector.salts-source")


class SaltSource:
    """Отдаёт матчи с готовой солью; качает их обычным путём."""

    name = "salts"

    def __init__(self, store, *, limit_per_cycle: int = 5,
                 timeout: float = 30.0, api_key: str | None = None,
                 shard: Shard | None = None) -> None:
        self._store = store
        self._limit = limit_per_cycle
        self._shard = shard or Shard()
        # Скачивание один в один как у остальных источников: свой
        # загрузчик означал бы вторую реализацию распаковки и проверки
        # целостности, а они уже дважды оказывались тонким местом
        # (zstd 2026-07-31, обрыв на 50–110 МиБ).
        self._downloader = OpenDotaSource(timeout=timeout, api_key=api_key)

    def fetch_new(self, after_cursor: str | None = None) -> Iterable[MatchRef]:
        """Курсор игнорируется намеренно — см. докстроку модуля."""
        # С запасом: часть выборки отсеет шардирование, и без запаса цикл
        # на двухмашинной конфигурации отдавал бы половину порции.
        rows = self._store.wanted(self._limit * 4)
        yielded = 0
        for match_id, url in rows:
            if yielded >= self._limit:
                break
            if not self._shard.accepts(match_id):
                continue
            yielded += 1
            yield MatchRef(
                match_id=match_id,
                replay_url=url,
                tier="Pub",
                # Курсор равен match_id, чтобы Collector записал его не
                # задумываясь. Читать его отсюда никто не будет.
                source_cursor=str(match_id),
            )
        if not yielded:
            # Пустая выдача и исправная работа выглядят одинаково, а
            # причины у них разные. Молчание здесь уже стоило проекту
            # тринадцати дней без бэкапов и целого спринта про пустую
            # очередь кандидатов (154).
            logger.info(
                "качать нечего: у всех матчей с добытой солью реплей уже "
                "есть. Соли добывает `make gc-salts` (ежечасно по cron)")

    def download_replay(self, ref: MatchRef) -> bytes:
        return self._downloader.download_replay(ref)
