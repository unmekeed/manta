"""Абстракция источника матчей — Anti-Corruption Layer (Гл. 2.5).

Каждый внешний источник (OpenDota, турнирные операторы, ...) приводит свою
модель данных к внутреннему типу MatchRef; ядро коллектора ничего не знает
о форматах чужих API (NFR-EXT-01).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class MatchRef:
    """Нормализованная ссылка на матч из внешнего источника."""

    match_id: int
    replay_url: str          # откуда скачивать .dem
    tier: str                # Pub | Premium | Professional | Tournament
    source_cursor: str       # позиция в источнике (для CollectorCursor)


def with_api_key(params: dict | None, api_key: str | None) -> dict:
    """Домешать OPENDOTA_API_KEY в query-параметры (снимает суточный лимит
    анонимного тарифа — см. docs/ROADMAP.md, D-раздел «rate limit»)."""
    params = dict(params) if params else {}
    if api_key:
        params["api_key"] = api_key
    return params


class Source(Protocol):
    """Контракт источника: имя + итератор новых матчей после курсора."""

    name: str

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        """Вернуть матчи новее переданного курсора (по порядку)."""
        ...

    def download_replay(self, ref: MatchRef) -> bytes:
        """Скачать содержимое .dem для матча."""
        ...
