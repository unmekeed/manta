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


@dataclass(frozen=True)
class Shard:
    """Разбиение потока кандидатов между независимыми машинами.

    Квота OpenDota считается по IP (~3000/сутки анонимно). Две+ машины с
    разными IP имеют независимые квоты — но, читая один и тот же список
    /parsedMatches сверху, обе схватят одни и те же свежие матчи. Чтобы
    не дублировать сбор (и не жечь квоту впустую), каждая машина берёт
    СВОЙ класс вычетов match_id по модулю: shard_id ∈ [0, count).

    count=1 (дефолт) — одиночная машина, фильтр пропускает всё. match_id
    монотонны и плотны, поэтому остатки делятся ~поровну. Координации
    между машинами не требуется: разбиение статично и детерминировано,
    пересечение множеств собранных матчей — пустое (слияние баз через
    dataset-import становится конфликт-фри).
    """

    shard_id: int = 0
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1 or not (0 <= self.shard_id < self.count):
            raise ValueError(
                f"некорректный шард {self.shard_id}/{self.count}")

    def accepts(self, match_id: int) -> bool:
        return self.count == 1 or match_id % self.count == self.shard_id


class Source(Protocol):
    """Контракт источника: имя + итератор новых матчей после курсора."""

    name: str

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        """Вернуть матчи новее переданного курсора (по порядку)."""
        ...

    def download_replay(self, ref: MatchRef) -> bytes:
        """Скачать содержимое .dem для матча."""
        ...
