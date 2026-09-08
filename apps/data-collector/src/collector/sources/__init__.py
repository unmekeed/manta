"""Абстракция источника матчей — Anti-Corruption Layer (Гл. 2.5).

Каждый внешний источник (OpenDota, турнирные операторы, ...) приводит свою
модель данных к внутреннему типу MatchRef; ядро коллектора ничего не знает
о форматах чужих API (NFR-EXT-01).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol

logger = logging.getLogger("collector.sources")


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


class IncompleteDownloadError(Exception):
    """Реплей скачался не полностью — обрыв соединения, не порча файла.

    Отдельный тип, потому что внешне это НЕ отличить от битого архива:
    распаковка обрезанного куска даёт то же «Invalid data stream». Разница
    в последствиях — обрыв надо повторить, а битый файл пропустить
    навсегда. Инцидент 2026-07-31: обрывы на 50–110 МиБ считались порчей,
    и реплейный путь выбрасывал исправные матчи один за другим.
    """


@dataclass(frozen=True)
class MatchRef:
    """Нормализованная ссылка на матч из внешнего источника."""

    match_id: int
    replay_url: str          # откуда скачивать .dem
    tier: str                # Pub | Premium | Professional | Tournament
    source_cursor: str       # позиция в источнике (для CollectorCursor)
    patch: int = 0           # id патча OpenDota; 0 — неизвестен (A9)

    # Средний ранг матча в шкале rank_tier (тир×10 + звезда); 0 —
    # неизвестен (спринт 199).
    #
    # ЗАЧЕМ ОН ЗДЕСЬ. `tier` у реплейного источника — КОНСТАНТА класса, а
    # не свойство матча: salts и candidates всегда ставят Pub. Проверить
    # это утверждение было нечем — витрина хранит avg_rank с миграции 018,
    # но заполняет его только JSON-путь, а реплейный оставлял ноль. То
    # есть у 85% матчей ярлык уровня не сверялся ни с чем.
    #
    # Ноль означает «не знаем», а не «низкий»: нулевого ранга не бывает.
    # Путать эти два значения нельзя — на них строится вывод о том,
    # верна ли разметка датасета.
    avg_rank: int = 0


def replay_source_tiers() -> dict[str, str]:
    """`source_name` → уровень матча для источников реплейного пути.

    ЗАЧЕМ ЭТО ЕСТЬ (спринт 195a). `CollectedMatches` хранит имя источника,
    но не уровень, а уровень нужен всякому, кто восстанавливает событие по
    уже собранному матчу — сегодня это `tools/republish_orphans.py`.

    Восстановление это ЧЕСТНОЕ, а не догадка: у каждого реплейного
    источника уровень — константа класса `TIER`, и в `MatchRef` попадает
    ровно она. Имя источника поэтому определяет уровень однозначно, и
    здесь берётся то же самое значение, а не второй его список.

    Импорт внутри функции, а не наверху модуля: источники импортируют
    `MatchRef` отсюда, и импорт на уровне модуля замкнул бы круг.
    """
    from .candidates import CandidateSource
    from .fixture import FixtureSource
    from .opendota import OpenDotaSource
    from .opendota_public import OpenDotaPublicSource
    from .parked import ParkedSource
    from .salts import SaltSource

    return {cls.name: cls.TIER for cls in (
        CandidateSource, FixtureSource, OpenDotaSource,
        OpenDotaPublicSource, ParkedSource, SaltSource)}


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


class PartnerSplit:
    """Доля потока, которая ВОЗВРАЩАЕТСЯ, когда напарник перестаёт собирать.

    ЖИВОЙ ОТКАЗ 6–7 сентября 2026. `SourceSplit` делил поток публичных
    матчей между OpenDota и STRATZ, а включалось деление по ФАКТУ НАЛИЧИЯ
    `STRATZ_API_TOKEN`. Токен протух: STRATZ получал 403 каждый цикл
    сутки подряд, контейнер при этом стоял `Up`, и половина кандидатов не
    собиралась НИКЕМ. Со стороны всё выглядело здоровым — второй источник
    честно отрабатывал свою половину и жаловаться ему было не на что.

    Проверялось присутствие настройки, а не пригодность источника. Ровно
    та же форма, что у тома, смонтированного не туда: объявление приняли
    за факт.

    ПО ЧЕМУ СУДИМ. По собранным матчам напарника, а не по его процессу,
    логам или расходу квоты. Расход не годится совсем: STRATZ потратил 52
    вызова в сутки, ни один из которых ничем не кончился, — счётчик
    попыток выглядит как жизнь. `CollectedMatches` же хранит РЕЗУЛЬТАТ, и
    вопрос «принёс ли напарник хоть один матч» отвечает на то, что нас
    действительно волнует.

    Мы намеренно НЕ разбираемся, ПОЧЕМУ напарник молчит. Протух токен,
    кончилась квота, некому отдавать кандидатов — для потока это одно и
    то же: доля простаивает. Диагноз ставит человек по логам, а решение
    «забрать долю» не должно его дожидаться.

    ПОЧЕМУ ЗАБИРАЕМ МЕДЛЕННО, А ОТДАЁМ СРАЗУ. Условие одно — возраст
    последнего матча напарника больше окна, — и оно даёт оба свойства
    само: чтобы забрать, нужно окно тишины; чтобы вернуть, достаточно
    одного собранного напарником матча. Обратная асимметрия была бы
    опасна: пока оба берут всё, они пишут в витрину одни и те же матчи, а
    она ReplacingMergeTree — строка STRATZ затирает строку OpenDota
    вместе с фичами трека F.

    ХОЛОДНЫЙ СТАРТ. Расширение требует не только молчания напарника, но и
    СВОЕЙ работы: у пустой базы не собрал никто, и без второго условия
    оба источника разом решили бы, что они одни, — и подрались бы ровно
    так, как деление и должно предотвращать.
    """

    def __init__(self, mine: "SourceSplit", my_name: str, partner_name: str,
                 last_collected, window_s: float = 21600.0,
                 refresh_s: float = 300.0, clock=None,
                 declined_by_partner=None) -> None:
        self._mine = mine
        self._me = my_name
        self._partner = partner_name
        self._probe = last_collected
        self._window = window_s
        self._refresh = refresh_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._solo = False
        self._checked_at: float | None = None
        # Матчи, которые напарник забрал и не смог (спринт 204). Их берём
        # СВЕРХ своей доли: иначе они не достаются никому.
        self._declined_probe = declined_by_partner
        self._declined: set[int] = set()

    # Своя доля видна снаружи так же, как у SourceSplit: это ЗАМЕНА, а не
    # обёртка сбоку. Читающий «какая у источника доля» не должен знать,
    # умеет ли она забирать чужую.
    @property
    def split_id(self) -> int:
        return self._mine.split_id

    @property
    def count(self) -> int:
        return self._mine.count

    def accepts(self, match_id: int) -> bool:
        if self._alone():
            return True
        if self._mine.accepts(match_id):
            return True
        # Чужая доля — но напарник по этому матчу СДАЛСЯ (спринт 204).
        #
        # Источники не равносильны на общих кандидатах: листинг
        # принадлежит OpenDota, и детали любого его матча она отдаст, а
        # STRATZ отдаст лишь те, что успел разобрать у себя. Замер
        # 08.09.2026: из доли STRATZ он не смог 70%, то есть 36% всего
        # потока не собирал НИКТО — фильтр доли отсекал эти матчи до
        # запроса деталей.
        #
        # Порядок проверок важен: своя доля сначала, чужая потом. Иначе
        # мы бы каждый раз спрашивали базу про матчи, которые и так наши.
        return match_id in self._declined_set()

    def _alone(self) -> bool:
        """Забрал ли я долю напарника; ответ кэшируется на refresh_s.

        Кэш обязателен: `accepts` вызывается на КАЖДОГО кандидата, а за
        ответом стоит поход в базу. Без кэша проверка живости стоила бы
        дороже самого сбора.
        """
        now = self._clock().timestamp()
        if self._checked_at is not None and now - self._checked_at < self._refresh:
            return self._solo
        self._checked_at = now
        was = self._solo
        self._solo = self._decide()
        self._declined = self._fetch_declined()
        if self._solo != was:
            logger.warning(
                "доля источника %s %s: последний собранный им матч старше "
                "%.0f ч", self._partner,
                "ЗАБРАНА" if self._solo else "возвращена", self._window / 3600)
        return self._solo

    def _declined_set(self) -> set[int]:
        """Отвергнутое напарником; обновляется на том же кэше, что живость.

        Отдельный поход в базу на каждого кандидата стоил бы дороже
        сбора: `accepts` вызывается сотни раз за цикл.
        """
        self._alone()          # обновит кэш, если пора
        return self._declined

    def _fetch_declined(self) -> set[int]:
        if self._declined_probe is None:
            return set()
        try:
            return self._declined_probe(self._partner)
        except Exception as exc:  # noqa: BLE001 — общая память не обязательна
            logger.warning("отказы напарника %s не прочитаны: %s",
                           self._partner, exc)
            # Прежнее множество, а не пустое: недоступная база — это
            # незнание, а не свидетельство, что напарник вдруг всё смог.
            return self._declined

    def _decide(self) -> bool:
        try:
            partner_last = self._probe(self._partner)
            my_last = self._probe(self._me)
        except Exception as exc:  # noqa: BLE001 — база молчит
            # Не знаем — значит не меняем расклад. Расширение по ошибке
            # чтения означало бы, что недоступная база разводит источники
            # драться за одни и те же матчи.
            logger.warning("живость напарника %s не проверена: %s",
                           self._partner, exc)
            return self._solo
        now = self._clock()
        partner_silent = (partner_last is None
                          or (now - partner_last).total_seconds() > self._window)
        i_work = (my_last is not None
                  and (now - my_last).total_seconds() <= self._window)
        return partner_silent and i_work


class Source(Protocol):
    """Контракт источника: имя + итератор новых матчей после курсора."""

    name: str

    def fetch_new(self, after_cursor: str | None) -> Iterable[MatchRef]:
        """Вернуть матчи новее переданного курсора (по порядку)."""
        ...

    def download_replay(self, ref: MatchRef) -> bytes:
        """Скачать содержимое .dem для матча."""
        ...
