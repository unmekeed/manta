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
import os
from typing import Iterable

import requests

from ..candidates import CandidateQueue
from . import MatchRef, PermanentDownloadError, Shard
from .opendota import OpenDotaSource
from .steam import ANONYMOUS_ACCOUNT_ID

logger = logging.getLogger("collector.candidates_source")

# Нижняя граница Immortal. Дублирует константу из ranks.py намеренно:
# ranks импортирует candidates, и обратный импорт замкнул бы цикл. У
# Immortal нет звёзд, поэтому rank_tier ровно 80 — значение стабильное,
# а не подобранное.
IMMORTAL_MIN_RANK = 80


class CandidateSource:
    name = "candidates"

    def __init__(self, queue: CandidateQueue, limit_per_cycle: int = 20,
                 base_url: str = "https://api.opendota.com/api",
                 timeout: float = 30.0, api_key: str | None = None,
                 shard: Shard | None = None, cache=None) -> None:
        self._queue = queue
        # Кэш рангов опционален: без него источник работает как прежде,
        # просто не подкармливает кэш фактическими рангами.
        self._cache = cache
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
            except requests.HTTPError as exc:
                status = (exc.response.status_code
                          if exc.response is not None else None)
                if status != 429:
                    self._queue.defer(cand.match_id, error=str(exc)[:200])
                    stats["нет соли"] += 1
                    continue
                # Исчерпанная квота — состояние ЦИКЛА, а не свойство
                # матча. Живой прогон 2026-08-06: 429 обрабатывался как
                # «у этого матча нет соли», цикл шёл к следующему
                # кандидату и сжигал шестьдесят запросов на пустом месте
                # («взято 60, отдано 0, нет соли 60»), дожигая и без того
                # отрицательный остаток. Хуже того, каждому из шестидесяти
                # прибавлялась попытка, и через восемь циклов очередь
                # молча превратилась бы в no_salt — из-за простоя, а не
                # из-за матчей.
                #
                # Пробрасываем наверх: в __main__ уже есть разбор 429 с
                # ожиданием до сброса квоты. Свой обработчик здесь только
                # мешал ему сработать.
                stats["квота исчерпана"] = 1
                self.last_cycle = stats
                logger.warning("цикл кандидатов оборван квотой OpenDota "
                               "после %d выдач: %s", stats["отдано"], exc)
                raise
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
            self._observe(cand.match_id, detail, stats)
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
        if not stats["взято"]:
            self._explain_idleness()

    def _explain_idleness(self) -> None:
        """Сказать, ПОЧЕМУ брать нечего (спринт 154).

        Пустая очередь и полностью собранная очередь дают одну и ту же
        строку «взято 0», и отличить их по логу было нельзя. На VPS это
        стоило дорого: коллектор поднят, ключ Steam вписан в env-файл,
        цикл каждые пять минут рапортует нулями — и всё выглядит рабочим.
        А очередь пуста с первого дня, потому что наполняет её `ranks
        scan`, которого нет ни в одном расписании.

        Молчание, неотличимое от успеха, в этом проекте уже стоило
        тринадцати дней без бэкапов. Здесь оно стоило бы всего датасета
        своей разбивки.
        """
        try:
            total = sum(self._queue.stats().values())
        except Exception:  # noqa: BLE001 — подсказка не должна ронять цикл
            return
        if total:
            return                      # очередь есть, просто вся выдана
        if not os.getenv("STEAM_API_KEY"):
            logger.warning(
                "очередь кандидатов пуста и наполнить её нечем: не задан "
                "STEAM_API_KEY. Своя разбивка не работает")
            return
        logger.warning(
            "очередь кандидатов пуста — её наполняет `python -m "
            "collector.ranks scan`, и он ни в одном расписании не стоит. "
            "Своя разбивка простаивает")

    def _observe(self, match_id: int, detail: dict | None,
                 stats: dict[str, int]) -> None:
        """Забрать из ответа за соль то, за что уже заплачено.

        В /matches/{id} лежит rank_tier КАЖДОГО игрока. Это разом две
        вещи, и обе бесплатны.

        Первая — проверка точности: правило берёт матч по двум известным
        рангам из десяти, и только факт покажет, не набираем ли мы мусор.

        Вторая важнее. Каждый скачанный матч отдаёт до десяти пар
        «аккаунт -> ранг» ПРЯМО В КЭШ. При тысяче с лишним закачек в
        сутки это порядка десяти тысяч рангов в день даром — больше, чем
        даёт вся суточная квота STRATZ. А чем полнее кэш, тем выше доля
        отбора, тем больше кандидатов: петля усиливает сама себя.
        """
        players = (detail or {}).get("players") or []
        ranks: dict[int, int] = {}
        known: list[int] = []
        for p in players:
            rank = int(p.get("rank_tier") or 0)
            if rank <= 0:
                continue
            known.append(rank)
            try:
                aid = int(p.get("account_id") or 0)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid != ANONYMOUS_ACCOUNT_ID:
                ranks[aid] = rank
        if not known:
            return
        high = sum(1 for r in known if r >= IMMORTAL_MIN_RANK)
        self._queue.record_truth(match_id, len(known), high,
                                 int(sum(known) / len(known)))
        stats["факт записан"] = stats.get("факт записан", 0) + 1
        if self._cache is not None and ranks:
            # Ранг из ответа актуален СЕЙЧАС — дата не указывается,
            # save() поставит текущее время.
            self._cache.save(ranks, "opendota-match")
            stats["рангов в кэш"] = stats.get("рангов в кэш", 0) + len(ranks)

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
