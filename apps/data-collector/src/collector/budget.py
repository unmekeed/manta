"""Суточный бюджет вызовов внешнего API (спринт 130).

Зачем. Квота OpenDota — 2000 запросов в сутки на IP, и купить больше
нельзя: платный тариф требует карты, недоступной владельцу. Это жёсткий
потолок и самый дефицитный ресурс проекта.

До сих пор он делился по принципу «кто успел», и 2026-08-06 это стоило
шести часов простоя ВСЕГО сбора. Новый конвейер кандидатов тратил ~2300
запросов в сутки при лимите 2000 — квота уходила в минус, и следом
ложились остальные коллекторы, включая те, что кормят про-эталон
промоушен-гейта. Один источник может остановить все.

Форма. Каждый коллектор — отдельный процесс, поэтому общий лимит нельзя
соблюдать поодиночке в памяти: счётчик общий и живёт в Postgres. Ключ
включает день UTC — ту же границу, на которой OpenDota сбрасывает свой
счётчик, — поэтому таблица обнуляется сама.

Исчерпанный бюджет — это состояние ЦИКЛА, а не свойство матча. Урок
дважды повторённой ошибки (спринты 126.1 и 129.1): источник обязан
прервать цикл и дать исключению всплыть до __main__, где уже есть
ожидание до сброса квоты. Поэтому `spend()` бросает, а не возвращает
False: возвращаемое значение слишком легко проигнорировать.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import psycopg

logger = logging.getLogger("collector.budget")

# Суточная квота анонимного тарифа OpenDota. Не наш выбор — их лимит.
OPENDOTA_DAILY_LIMIT = 2000

# Наш общий потолок. Ниже квоты намеренно: запас в сотню вызовов покрывает
# гонку между процессами (проверка и инкремент — два запроса) и разовые
# ретраи, которые считаются не везде.
GLOBAL_LIMIT = int(os.getenv("OPENDOTA_GLOBAL_LIMIT", "1900"))

# Гарантированные доли источников. Каноническое место — здесь, а не в
# dev-recover: чтобы одолжить неиспользуемое, процесс обязан знать доли
# СОСЕДЕЙ, а из своей переменной окружения он их не видит.
# OPENDOTA_BUDGET по-прежнему переопределяет долю СВОЕГО процесса.
SHARES = {
    "candidates": 1100,
    "opendota-timeline": 250,
    "opendota-timeline-pro": 200,
    "opendota-league": 200,
    "opendota-public": 50,
    "opendota": 50,             # про-реплеи
    "stratz-timeline": 50,
}


class BudgetExhausted(RuntimeError):
    """Источник выбрал свою долю суточной квоты.

    Отдельный тип, а не общий RuntimeError: __main__ должен отличать его
    от поломки и уснуть до сброса, а не сыпать стектрейсами каждый цикл.
    """


class ApiBudget:
    """Счётчик вызовов одного источника к одному API за сутки UTC."""

    def __init__(self, dsn: str, source: str, limit: int,
                 api: str = "opendota", shares: dict[str, int] | None = None,
                 global_limit: int = 0) -> None:
        self._dsn = dsn
        self._db = psycopg.connect(dsn, autocommit=True)
        self._source = source
        self._limit = max(0, limit)
        self._api = api
        self._shares = dict(SHARES if shares is None else shares)
        self._global = global_limit or GLOBAL_LIMIT

    def close(self) -> None:
        self._db.close()

    def _today(self) -> str:
        # День UTC, а не локальный: OpenDota сбрасывает счётчик в 00:00
        # UTC, и считать по московскому календарю значило бы сбрасывать
        # бюджет на три часа раньше и получать 429 на ровном месте.
        return datetime.now(timezone.utc).date().isoformat()

    def _day_left(self) -> float:
        """Какая доля суток UTC ещё впереди, [0, 1]."""
        now = datetime.now(timezone.utc)
        passed = now.hour * 3600 + now.minute * 60 + now.second
        return max(0.0, 1.0 - passed / 86400.0)

    def _used_by_source(self) -> dict[str, int]:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT source, calls FROM ApiBudget "
                " WHERE day = %s AND api = %s", (self._today(), self._api))
            return {str(src): int(n) for src, n in cur.fetchall()}

    def cap(self, used_by_source: dict[str, int]) -> int:
        """Сколько ЭТОТ источник вправе потратить за сутки прямо сейчас.

        Жёсткая доля оказалась хуже болезни, которую лечила. Замер
        2026-08-07 08:24 UTC: `stratz-timeline` (53/50) и
        `opendota-timeline` (253/250) спали до полуночи, выбрав свои
        крошечные доли, — а общая квота была цела на 85%, и приток из
        всех источников, кроме реплейного, стоял с трёх ночи. Спринт 130
        чинил «один источник выедает всё» и создал «источники стоят при
        свободной квоте».

        Правило теперь такое: доля — ГАРАНТИЯ, а не потолок. Сверх неё
        можно брать из общего пула, но пул сначала резервирует
        невыбранные гарантии соседей — иначе кандидаты за утро съели бы
        всё, включая долю про-эталона, от которого зависит гейт
        продвижения.

        Резерв ТАЕТ вместе с сутками: в 00:30 невыбранная гарантия почти
        целиком удержана (сосед ещё успеет ею воспользоваться), к 20:00 —
        едва на шестую часть. Молчащий весь день источник не должен
        держать квоту мёртвым грузом до полуночи.

        Чего это правило НЕ обещает. Гарантия сильна ровно настолько,
        насколько велик резерв, а он тает вместе с сутками: к вечеру
        сосед вправе занять чужую невыбранную долю, и проснувшийся позже
        получит меньше номинала. Пол «не ниже своей доли» здесь стоял в
        первой редакции и был убран тестом: с ним сумма выданного
        перебивала общий пул, а пул — это чужой лимит, за которым 429 на
        весь день. Внешнее ограничение жёстче нашей политики.
        """
        others_used = sum(v for k, v in used_by_source.items()
                          if k != self._source)
        unclaimed = sum(max(0, share - used_by_source.get(name, 0))
                        for name, share in self._shares.items()
                        if name != self._source)
        reserve = unclaimed * self._day_left()
        return max(0, int(self._global - others_used - reserve))

    def spend(self, n: int = 1) -> int:
        """Записать вызов. Бросает BudgetExhausted, когда брать нечего.

        Инкремент — ОДНИМ запросом: два процесса одного источника
        (например, после неудачного перезапуска) иначе оба прочитали бы
        «ещё можно» и вместе перебрали лимит. Потолок считается вторым
        запросом, и здесь возможен небольшой перелёт при одновременной
        работе нескольких коллекторов — на него и оставлен запас между
        GLOBAL_LIMIT и настоящей квотой.
        """
        if self._limit <= 0:
            return 0
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO ApiBudget (day, api, source, calls) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (day, api, source) DO UPDATE "
                "  SET calls = ApiBudget.calls + EXCLUDED.calls "
                "RETURNING calls",
                (self._today(), self._api, self._source, n))
            used = int(cur.fetchone()[0])
        allowed = self.cap({**self._used_by_source(), self._source: used})
        if used > allowed:
            # Откат. Инкремент идёт ПЕРЕД проверкой (иначе два процесса
            # одного источника оба прочитали бы «ещё можно»), но отказ не
            # должен стоить вызова: коллектор в этот момент никуда не
            # ходил.
            #
            # Без отката счётчик рос от каждой отвергнутой попытки, и
            # источник в цикле повторов раздувал и свою строку, и общий
            # пул. Это ровно то, что видно в поле 2026-08-07: ApiBudget
            # насчитал 1304 вызова за сутки, а сама OpenDota — 304.
            with self._db.cursor() as cur:
                cur.execute(
                    "UPDATE ApiBudget SET calls = calls - %s "
                    " WHERE day = %s AND api = %s AND source = %s",
                    (n, self._today(), self._api, self._source))
            raise BudgetExhausted(
                f"{self._source}: общий бюджет {self._api} исчерпан "
                f"({used - n}/{allowed}, гарантия {self._limit}, "
                f"пул {self._global})")
        return used

    def used(self) -> int:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT coalesce(sum(calls), 0) FROM ApiBudget "
                " WHERE day = %s AND api = %s AND source = %s",
                (self._today(), self._api, self._source))
            return int(cur.fetchone()[0])


# =============================================================================
# Модульный синглтон
# =============================================================================
#
# Каждый процесс коллектора обслуживает РОВНО ОДИН источник, поэтому
# бюджет знает своё имя и на месте вызова остаётся одна строка `spend()`.
# Альтернатива — протаскивать объект в конструкторы шести источников и в
# каждый вызов — дала бы столько же связности при большем шуме в коде.
#
# Не настроен — no-op. Так тесты и фикстурный источник работают без БД, а
# забытая настройка не роняет сбор, а лишь не считает.

_current: ApiBudget | None = None


def configure(dsn: str, source: str, limit: int,
              api: str = "opendota") -> ApiBudget | None:
    global _current
    if limit <= 0:
        logger.info("бюджет %s для %s не задан — учёт выключен", api, source)
        _current = None
        return None
    _current = ApiBudget(dsn, source, limit, api)
    logger.info("бюджет %s для %s: %d вызовов в сутки (потрачено %d)",
                api, source, limit, _current.used())
    return _current


def spend(n: int = 1) -> None:
    """Отметить вызов внешнего API. No-op, если бюджет не настроен."""
    if _current is not None:
        _current.spend(n)


def reset_for_tests() -> None:
    global _current
    _current = None


def budget_from_env(dsn: str, source: str) -> ApiBudget | None:
    """Гарантированная доля ЭТОГО процесса в суточной квоте.

    По умолчанию берётся из SHARES — каноническое место, потому что для
    заимствования свободного пула процессу нужны доли соседей, а из своей
    переменной окружения он их не увидит. OPENDOTA_BUDGET переопределяет
    гарантию этого процесса и оставлен ради разовых экспериментов и
    обратной совместимости с dev-recover.
    """
    default = SHARES.get(source, 0)
    return configure(dsn, source,
                     int(os.getenv("OPENDOTA_BUDGET", str(default))))
