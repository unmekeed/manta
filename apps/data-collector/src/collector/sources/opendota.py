"""Источник OpenDota: pull-режим по /proMatches (Гл. 3.3).

Схема получения реплея:
1. GET /proMatches — последние профессиональные матчи (без replay_salt);
2. GET /matches/{id} — детали матча, включая готовый replay_url
   (http://replayN.valve.net/570/{id}_{salt}.dem.bz2);
3. скачивание и распаковка bz2 → сырой .dem (магия PBDEMS2 проверяется).

Лимит матчей за цикл ограничивает и нагрузку на бесплатный тариф
OpenDota (60 запросов/мин), и трафик с реплей-серверов Valve
(~50-100 МиБ на матч в bz2).
"""
from __future__ import annotations

import bz2
import logging
import time
from typing import Iterable

import requests

from . import MatchRef

logger = logging.getLogger("collector.opendota")

DEM_MAGIC = b"PBDEMS2"


class OpenDotaSource:
    name = "opendota"

    def __init__(self, base_url: str = "https://api.opendota.com/api",
                 limit_per_cycle: int = 3, timeout: float = 30.0,
                 api_delay_s: float = 1.1) -> None:
        self._base = base_url.rstrip("/")
        self._limit = limit_per_cycle
        self._timeout = timeout
        self._api_delay_s = api_delay_s  # бережём rate limit бесплатного тарифа

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        resp = requests.get(f"{self._base}/proMatches", timeout=self._timeout)
        resp.raise_for_status()
        floor = int(after_cursor) if after_cursor else 0
        # Старые вперёд: курсор растёт монотонно, прерванный цикл
        # продолжается с места остановки.
        rows = sorted((r for r in resp.json() if int(r["match_id"]) > floor),
                      key=lambda r: int(r["match_id"]))
        yielded = 0
        for row in rows:
            if yielded >= self._limit:
                break
            match_id = int(row["match_id"])
            detail = self._match_detail(match_id)
            replay_url = (detail or {}).get("replay_url")
            if not replay_url:
                # Реплей ещё не выложен (salt появляется с задержкой) —
                # матч будет подобран следующим циклом, курсор не двигаем.
                logger.info("match %s: replay not ready yet, stopping cycle",
                            match_id)
                break
            yielded += 1
            yield MatchRef(
                match_id=match_id,
                replay_url=replay_url,
                tier="Professional",
                source_cursor=str(match_id),
            )

    def _match_detail(self, match_id: int) -> dict | None:
        time.sleep(self._api_delay_s)
        resp = requests.get(f"{self._base}/matches/{match_id}",
                            timeout=self._timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def download_replay(self, ref: MatchRef) -> bytes:
        logger.info("downloading %s", ref.replay_url)
        resp = requests.get(ref.replay_url, timeout=600.0)
        resp.raise_for_status()
        data = resp.content
        if ref.replay_url.endswith(".bz2"):
            data = bz2.decompress(data)
        if not data.startswith(DEM_MAGIC):
            raise ValueError(
                f"match {ref.match_id}: not a Source 2 demo "
                f"(magic {data[:8]!r})")
        logger.info("match %s: %.1f MiB decompressed",
                    ref.match_id, len(data) / 1048576)
        return data
