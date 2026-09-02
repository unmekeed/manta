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
import time
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


# -- месячный потолок (спринт 183) ---------------------------------------------
#
# ЗАЧЕМ ОТДЕЛЬНО ОТ СУТОЧНОГО. Платный тариф OpenDota считает деньги ЗА
# МЕСЯЦ ($0.01 за 100 вызовов), а суточный лимит про месяц ничего не
# знает: 15 000 в сутки — это $46,5 в месяце из 31 дня, и никто об этом
# не скажет. Разница между «квота кончилась» и «пришёл счёт» в том, что
# первое видно сразу, а второе — в конце месяца и деньгами.
#
# 0 — потолка нет, и это верное умолчание для БЕСПЛАТНОГО тарифа: там
# суточный лимит и так не даёт потратить деньги, потому что денег нет.
# А вот на ключе выключенный месячный потолок — тихая дыра, поэтому
# configure() про это предупреждает вслух.
MONTHLY_LIMIT = int(os.getenv("OPENDOTA_MONTHLY_LIMIT", "0"))

# Цена вызова на платном тарифе: $0.01 за 100. Держится числом, потому
# что расход в вызовах владельцу не говорит ничего, а в долларах —
# говорит всё.
COST_PER_CALL_USD = float(os.getenv("OPENDOTA_COST_PER_CALL", "0.0001"))

# Как часто пересчитывается месячная сумма. Спрашивать её на каждый вызов
# значило бы добавить запрос к базе на каждый запрос к API; минута даёт
# перелёт порядка десятка вызовов (то есть центов) ценой одного запроса в
# минуту.
MONTH_REFRESH_S = 60


class BudgetExhausted(RuntimeError):
    """Источник выбрал свою долю суточной квоты.

    Отдельный тип, а не общий RuntimeError: __main__ должен отличать его
    от поломки и уснуть до сброса, а не сыпать стектрейсами каждый цикл.
    """


class MonthlyBudgetExhausted(BudgetExhausted):
    """Выбран МЕСЯЧНЫЙ потолок — деньги, а не квота.

    Наследник, а не отдельный тип: для __main__ это тот же случай
    «сегодня брать нечего, спи». Но имя своё, потому что причина другая
    и лечится иначе — сутки его не сбросят, нужен либо новый месяц, либо
    решение владельца поднять потолок.
    """


class ApiBudget:
    """Счётчик вызовов одного источника к одному API за сутки UTC."""

    # Умолчания НА УРОВНЕ КЛАССА, а не только в __init__: объект собирают
    # и через __new__ (так делают тесты, чтобы подсунуть своё соединение),
    # и такой экземпляр не должен падать на отсутствующем атрибуте.
    # Поймано существующими тестами при добавлении месячного потолка.
    _monthly = 0
    _month_cache = (0.0, 0)

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
        self._monthly = MONTHLY_LIMIT
        self._month_cache = (0.0, 0)     # (момент замера, вызовов за месяц)

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

    def _month_start(self) -> str:
        """Первое число текущего месяца UTC.

        Тариф считает календарный месяц, поэтому и мы считаем его же — а
        не «последние тридцать дней». Скользящее окно давало бы расход,
        не совпадающий со счётом, и спорить с провайдером пришлось бы
        своими цифрами против его.
        """
        now = datetime.now(timezone.utc)
        return now.date().replace(day=1).isoformat()

    def month_used(self, force: bool = False) -> int:
        """Вызовов к этому API с начала месяца — всеми источниками.

        Потолок общий на API, а не на процесс: платит владелец за сумму,
        и делить его по коллекторам значило бы разрешить шестерым выбрать
        шесть потолков.
        """
        now = time.monotonic()
        ts, value = self._month_cache
        if not force and now - ts < MONTH_REFRESH_S:
            return value
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT coalesce(sum(calls), 0) FROM ApiBudget "
                " WHERE day >= %s AND api = %s",
                (self._month_start(), self._api))
            value = int(cur.fetchone()[0])
        self._month_cache = (now, value)
        return value

    def month_cost_usd(self) -> float:
        return self.month_used() * COST_PER_CALL_USD

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
        # Месячный потолок проверяется ДО инкремента, в отличие от
        # суточного. Суточный терпит перелёт (на него оставлен запас до
        # настоящей квоты), а здесь перелёт — это деньги, и списывать
        # вызов, которого не будет, незачем.
        if self._monthly > 0:
            month = self.month_used()
            if month + n > self._monthly:
                raise MonthlyBudgetExhausted(
                    f"{self._source}: МЕСЯЧНЫЙ потолок {self._api} выбран "
                    f"({month}/{self._monthly} вызовов, "
                    f"${month * COST_PER_CALL_USD:.2f}). Сутки его не "
                    f"сбросят — нужен новый месяц или другое значение "
                    f"OPENDOTA_MONTHLY_LIMIT")
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
    if MONTHLY_LIMIT > 0:
        spent = _current.month_used(force=True)
        logger.info("месячный потолок %s: %d вызовов ($%.2f), "
                    "потрачено %d ($%.2f)", api, MONTHLY_LIMIT,
                    MONTHLY_LIMIT * COST_PER_CALL_USD,
                    spent, spent * COST_PER_CALL_USD)
    elif os.getenv("OPENDOTA_API_KEY"):
        # Ключ есть — значит тариф платный и вызовы стоят денег. Молчать
        # тут нельзя: «потолка нет» на бесплатном тарифе безобидно, а на
        # платном это единственное, что стоит между расписанием и счётом.
        logger.warning(
            "OPENDOTA_API_KEY задан, а OPENDOTA_MONTHLY_LIMIT — нет: "
            "расход за месяц ничем не ограничен, а вызовы платные "
            "($%.4f за вызов)", COST_PER_CALL_USD)
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
