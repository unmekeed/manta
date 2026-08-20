"""Источник «припаркованные реплеи» (спринт 153).

Возврат к матчам, чей реплей однажды не удалось взять. Отдельный источник,
а не флаг у существующего, по двум причинам.

Первая: весь конвейер уже написан. Скачивание, выгрузка в S3, событие в
Kafka, отметка в CollectedMatches — всё это делает Collector.collect_once,
и ему безразлично, откуда пришла ссылка. Источнику остаётся отдать
MatchRef, и повторный заход получается бесплатно.

Вторая: у припаркованных матчей нет курсора и быть не может. Курсор
описывает движение вперёд по ленте источника, а здесь лента — список
желаний, из которого исполненные исчезают сами. Смешав это с обычным
источником, пришлось бы объяснять, что значит его курсор, и объяснение
вышло бы неправдой.

Запускается вручную и по требованию:

    COLLECTOR_SOURCE=parked python -m collector --once

Ничего не паркует сам: если матч снова не взялся, ParkedStore лишь
увеличит счётчик попыток, и он останется на месте.
"""
from __future__ import annotations

import logging
from typing import Iterable

from . import MatchRef, Shard
from .opendota import OpenDotaSource

logger = logging.getLogger("collector.parked-source")


class ParkedSource:
    """Отдаёт припаркованные матчи; скачивает их обычным путём OpenDota."""

    name = "parked"

    def __init__(self, store, *, limit_per_cycle: int = 5,
                 timeout: float = 30.0, api_key: str | None = None,
                 shard: Shard | None = None) -> None:
        self._store = store
        self._limit = limit_per_cycle
        self._shard = shard or Shard()
        # Скачивание один в один как у обычного источника: свой загрузчик
        # здесь означал бы вторую реализацию распаковки и проверки
        # целостности, а они уже дважды оказывались тонким местом
        # (zstd 2026-07-31, обрыв на 50–110 МиБ).
        self._downloader = OpenDotaSource(timeout=timeout, api_key=api_key)

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        """Курсор игнорируется намеренно — см. докстроку модуля."""
        rows = self._store.wanted(self._limit * 4)
        yielded = 0
        for match_id, replay_url in rows:
            if yielded >= self._limit:
                break
            if not self._shard.accepts(match_id):
                continue
            yielded += 1
            yield MatchRef(
                match_id=match_id,
                replay_url=replay_url,
                tier="Professional",
                # Курсор ставим равным match_id, чтобы Collector мог его
                # записать не задумываясь. Читать его отсюда никто не
                # будет: fetch_new его не смотрит.
                source_cursor=str(match_id),
            )
        if not yielded:
            logger.info("парковка пуста — возвращаться не к чему")

    def download_replay(self, ref: MatchRef) -> bytes:
        return self._downloader.download_replay(ref)
