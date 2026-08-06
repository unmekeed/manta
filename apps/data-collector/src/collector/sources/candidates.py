"""Источник матчей из очереди кандидатов (спринт 126).

Своя разбивка целиком: матчи находит ranks-scan по потоку Valve и кэшу
рангов, здесь они превращаются в MatchRef и уходят в тот же конвейер,
что и всё остальное — S3, Kafka, C++ ядро, витрина. Ничего нового ниже
по течению не строится, и это главное достоинство такой формы.

От OpenDota остаётся ровно ОДНА вещь — соль реплея. Ни список матчей, ни
ранги у него больше не спрашиваются: список даёт Valve бесплатно, ранги
приходят из кэша. Один запрос на матч против прежних двух-трёх, то есть
суточная квота OpenDota (~2000) перестаёт быть потолком датасета и
становится ровно потолком скачивания — а он и так задан каналом.

Тонкость, из-за которой источник не может быть простым SELECT: у свежего
матча соли ещё нет. Это НЕ ошибка и не повод сдвигать курсор — матч надо
отложить и вернуться к нему позже, чем и занимается CandidateQueue.defer.
"""
from __future__ import annotations

import logging
from typing import Iterable

import requests

from ..candidates import CandidateQueue
from . import MatchRef, PermanentDownloadError, Shard
from .opendota import OpenDotaSource

logger = logging.getLogger("collector.candidates_source")


class CandidateSource:
    name = "candidates"

    def __init__(self, queue: CandidateQueue, limit_per_cycle: int = 20,
                 base_url: str = "https://api.opendota.com/api",
                 timeout: float = 30.0, api_key: str | None = None,
                 shard: Shard | None = None) -> None:
        self._queue = queue
        self._limit = limit_per_cycle
        self._shard = shard or Shard()
        # Скачивание и распаковка — те же, что у остальных источников:
        # формат .dem меняется (bz2 -> zstd), и знать об этом должно одно
        # место, а не каждый источник по отдельности.
        self._od = OpenDotaSource(base_url=base_url, timeout=timeout,
                                  api_key=api_key)
        self.last_cycle: dict[str, int] = {}

    def fetch_new(self, after_cursor: str | None = None) -> Iterable[MatchRef]:
        stats = {"взято": 0, "отдано": 0, "нет соли": 0, "безнадёжных": 0,
                 "просрочено": 0, "зависших вернули": 0}
        stats["просрочено"] = self._queue.expire()
        # Кандидат, выданный процессу, который затем умер (рестарт WSL,
        # kill, падение), остался бы в `taken` навсегда. Возвращаем такие
        # в очередь — это стандартный visibility timeout, без него
        # очередь медленно протекает при каждом перезапуске.
        stats["зависших вернули"] = self._queue.requeue_stale_taken()

        # Берём с запасом: часть кандидатов уйдёт в отложенные из-за
        # отсутствия соли, и без запаса цикл отдал бы пустоту при полной
        # очереди.
        taken = self._queue.take(self._limit * 3)
        stats["взято"] = len(taken)
        for cand in taken:
            if stats["отдано"] >= self._limit:
                break
            if not self._shard.accepts(cand.match_id):
                continue
            try:
                detail = self._od._match_detail(cand.match_id)
            except requests.RequestException as exc:
                # Сетевой сбой — не приговор матчу, но и не повод
                # тратить цикл: откладываем и идём дальше.
                self._queue.defer(cand.match_id, error=str(exc)[:200])
                stats["нет соли"] += 1
                continue
            replay_url = (detail or {}).get("replay_url")
            if not replay_url:
                state = self._queue.defer(cand.match_id, error="нет replay_url")
                stats["безнадёжных" if state == "no_salt" else "нет соли"] += 1
                continue
            self._queue.mark(cand.match_id, "taken")
            stats["отдано"] += 1
            yield MatchRef(
                match_id=cand.match_id,
                replay_url=replay_url,
                tier="Pub",
                source_cursor=str(cand.match_id),
                patch=int((detail or {}).get("patch") or 0),
            )
        self.last_cycle = stats
        logger.info("цикл кандидатов: %s",
                    ", ".join(f"{k} {v}" for k, v in stats.items()))

    def download_replay(self, ref: MatchRef) -> bytes:
        """Скачать реплей и закрыть кандидата — в ЛЮБОМ исходе.

        Первый живой прогон вскрыл утечку: кандидат помечался `taken` при
        выдаче, а при сбое скачивания там и оставался навсегда. Очередь
        выбирает только `new`, поэтому такой матч не повторялся никогда и
        тихо терялся — за полтора часа так зависло шесть штук (418 от
        реплей-сервера Valve).

        Поэтому каждая ветка исхода что-то делает с состоянием:
        постоянная ошибка закрывает кандидата навсегда, временная
        возвращает в очередь с отложенным повтором.
        """
        try:
            data = self._od.download_replay(ref)
        except PermanentDownloadError as exc:
            # Реплей уже не скачать никогда: 404/410, битый архив, не
            # демка. Возвращать в очередь нечего.
            self._queue.mark(ref.match_id, "failed", str(exc)[:200])
            raise
        except Exception as exc:  # noqa: BLE001 — состояние важнее типа
            self._queue.defer(ref.match_id, error=str(exc)[:200])
            raise
        # Отмечаем ПОСЛЕ успешного скачивания, а не при выдаче: между
        # ними лежит 58 МиБ по сети, и обрыв не должен выглядеть как
        # успешно собранный матч.
        self._queue.mark(ref.match_id, "done")
        return data
