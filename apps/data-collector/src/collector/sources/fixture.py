"""Fixture-источник для разработки и тестов: детерминированные матчи."""
from __future__ import annotations

import os
from typing import Iterable

from . import MatchRef


class FixtureSource:
    name = "fixture"

    def __init__(self, matches: list[MatchRef] | None = None) -> None:
        self._matches = matches if matches is not None else [
            MatchRef(
                match_id=8000000001 + i,
                replay_url=f"fixture://replays/{8000000001 + i}.dem",
                tier="Pub",
                source_cursor=str(8000000001 + i),
            )
            for i in range(3)
        ]

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        threshold = int(after_cursor) if after_cursor else 0
        for ref in self._matches:
            if ref.match_id > threshold:
                yield ref

    def download_replay(self, ref: MatchRef) -> bytes:
        # Детерминированное псевдосодержимое: заголовок Source 2 + мусор.
        return b"PBDEMS2\x00" + os.urandom(1024)
