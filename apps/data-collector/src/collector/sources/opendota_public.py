"""Источник OpenDota: высокоранговые ранкед-матчи ТЕКУЩЕГО патча.

Отличия от pro-источника (opendota.py):
- /publicMatches?min_rank=80 (Immortal-скобка) вместо /proMatches —
  матчей на порядки больше, датасет растёт быстро;
- фильтр качества данных для обучения: ranked matchmaking
  (lobby_type=7), All Pick (game_mode=22 — без турбо, который ломает
  экономические закономерности), минимальная длительность;
- фильтр по патчу: /constants/patch → последний id; матчи старых
  патчей отбрасываются (модель обучается на актуальной мете);
- у самых свежих матчей OpenDota ещё не имеет replay_salt, поэтому
  окно кандидатов сдвинуто назад (lag_matches по match_id), а матч без
  реплея ПРОПУСКАЕТСЯ (в отличие от pro-источника, где реплей
  гарантированно появится и надо ждать): паблик-матчей достаточно,
  чтобы не ждать ни один конкретный.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

import requests

from . import MatchRef, Shard, with_api_key
from .opendota import DEM_MAGIC, OpenDotaSource

logger = logging.getLogger("collector.opendota_public")


class OpenDotaPublicSource:
    name = "opendota_public"

    def __init__(self, base_url: str = "https://api.opendota.com/api",
                 limit_per_cycle: int = 5, min_rank: int = 80,
                 min_patch: int | None = None, min_duration_s: int = 900,
                 lag_matches: int = 60_000, timeout: float = 30.0,
                 api_delay_s: float = 1.1, api_key: str | None = None,
                 shard: Shard | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._limit = limit_per_cycle
        self._min_rank = min_rank
        self._min_patch = min_patch  # None → определить последний патч
        self._min_duration_s = min_duration_s
        self._lag = lag_matches
        self._timeout = timeout
        self._api_delay_s = api_delay_s
        self._api_key = api_key
        self._shard = shard or Shard()
        # Скачивание/распаковка идентичны pro-источнику.
        self._downloader = OpenDotaSource(base_url=base_url, timeout=timeout,
                                          api_key=api_key)

    # -- патч ------------------------------------------------------------------

    def _latest_patch(self) -> int:
        resp = requests.get(f"{self._base}/constants/patch",
                            params=with_api_key(None, self._api_key),
                            timeout=self._timeout)
        resp.raise_for_status()
        patches = resp.json()
        latest = max(p["id"] for p in patches)
        name = next(p["name"] for p in patches if p["id"] == latest)
        logger.info("актуальный патч: %s (id=%d)", name, latest)
        return latest

    # -- выборка ---------------------------------------------------------------

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        if self._min_patch is None:
            self._min_patch = self._latest_patch()

        # Верхняя граница окна: свежайший матч минус лаг — у более новых
        # OpenDota обычно ещё не имеет replay_salt.
        newest = requests.get(
            f"{self._base}/publicMatches",
            params=with_api_key({"min_rank": str(self._min_rank)},
                                self._api_key),
            timeout=self._timeout)
        newest.raise_for_status()
        rows = newest.json()
        if not rows:
            return
        ceiling = max(int(r["match_id"]) for r in rows) - self._lag

        time.sleep(self._api_delay_s)
        resp = requests.get(
            f"{self._base}/publicMatches",
            params=with_api_key({"min_rank": str(self._min_rank),
                                 "less_than_match_id": str(ceiling)},
                                self._api_key),
            timeout=self._timeout)
        resp.raise_for_status()

        floor = int(after_cursor) if after_cursor else 0
        candidates = sorted(
            (r for r in resp.json()
             if int(r["match_id"]) > floor
             and r.get("lobby_type") == 7
             and r.get("game_mode") == 22
             and int(r.get("duration", 0)) >= self._min_duration_s),
            key=lambda r: int(r["match_id"]))

        yielded = 0
        for row in candidates:
            if yielded >= self._limit:
                break
            match_id = int(row["match_id"])
            if not self._shard.accepts(match_id):
                continue                     # чужой шард — не тратим квоту
            detail = self._match_detail(match_id)
            if not detail:
                continue
            if int(detail.get("patch") or 0) < self._min_patch:
                logger.info("match %s: старый патч %s, пропуск",
                            match_id, detail.get("patch"))
                continue
            replay_url = detail.get("replay_url")
            if not replay_url:
                logger.info("match %s: реплей ещё не выложен, пропуск", match_id)
                continue
            yielded += 1
            yield MatchRef(
                match_id=match_id,
                replay_url=replay_url,
                tier="Premium",  # высокоранговый ранкед (Гл. 4.2 tier-схема)
                source_cursor=str(match_id),
                patch=int(detail.get("patch") or 0),
            )

    def _match_detail(self, match_id: int) -> dict | None:
        time.sleep(self._api_delay_s)
        resp = requests.get(f"{self._base}/matches/{match_id}",
                            params=with_api_key(None, self._api_key),
                            timeout=self._timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def download_replay(self, ref: MatchRef) -> bytes:
        return self._downloader.download_replay(ref)


__all__ = ["OpenDotaPublicSource", "DEM_MAGIC"]
