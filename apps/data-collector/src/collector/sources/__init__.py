"""Абстракция источника матчей — Anti-Corruption Layer (Гл. 2.5).

Каждый внешний источник (OpenDota, турнирные операторы, ...) приводит свою
модель данных к внутреннему типу MatchRef; ядро коллектора ничего не знает
о форматах чужих API (NFR-EXT-01).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


class PermanentDownloadError(Exception):
    """Реплей этого матча не удастся скачать НИКОГДА.

    Отделено от временных сбоев (таймаут, 5xx, обрыв сети) намеренно: по
    временной ошибке матч имеет смысл повторить, а по постоянной — нет,
    и коллектор обязан сдвинуть курсор дальше. Иначе источник встаёт
    навсегда на первом же неисправимом матче (инцидент 2026-07-31:
    реплейный путь стоял 82ч на битом bz2 при зелёном pgrep).

    Постоянные случаи: Valve отдаёт 404/410 (реплей уже удалён с
    серверов — они хранятся ограниченное время), битый bz2, содержимое
    не является демкой Source 2.
    """


@dataclass(frozen=True)
class MatchRef:
    """Нормализованная ссылка на матч из внешнего источника."""

    match_id: int
    replay_url: str          # откуда скачивать .dem
    tier: str                # Pub | Premium | Professional | Tournament
    source_cursor: str       # позиция в источнике (для CollectorCursor)
    patch: int = 0           # id патча OpenDota; 0 — неизвестен (A9)


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


@dataclass(frozen=True)
class SourceSplit:
    """Разделение кандидатов между источниками деталей ОДНОЙ машины.

    Shard разводит машины, а этот фильтр — источники внутри машины.
    Нужен, потому что JSON-источники читают ОДИН И ТОТ ЖЕ листинг с
    вершины: без разделения opendota-timeline и stratz-timeline каждый
    цикл дерутся за одни и те же свежие матчи. CollectedMatches отсекает
    повтор только ПОСЛЕ того, как матч отмечен, а между проверкой и
    отметкой лежит запрос деталей — в это окно оба успевают взять матч.

    Цена такой ничьей не «лишний запрос», а потеря фич: витрина —
    ReplacingMergeTree по (match_id, game_time), и побеждает строка,
    вставленная последней. Приди она от STRATZ, она затрёт строку
    OpenDota вместе с фичами трека F, которых у STRATZ нет.

    Делится по `match_id // 10`, а не по остатку самого id: младшие
    разряды уже заняты межмашинным Shard, и общий делитель сцепил бы два
    фильтра (машина №2 не получала бы половину источников вовсе).
    """

    split_id: int = 0
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1 or not (0 <= self.split_id < self.count):
            raise ValueError(
                f"некорректное разделение {self.split_id}/{self.count}")

    def accepts(self, match_id: int) -> bool:
        return self.count == 1 or (match_id // 10) % self.count == self.split_id


class Source(Protocol):
    """Контракт источника: имя + итератор новых матчей после курсора."""

    name: str

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        """Вернуть матчи новее переданного курсора (по порядку)."""
        ...

    def download_replay(self, ref: MatchRef) -> bytes:
        """Скачать содержимое .dem для матча."""
        ...
