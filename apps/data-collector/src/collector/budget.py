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


class BudgetExhausted(RuntimeError):
    """Источник выбрал свою долю суточной квоты.

    Отдельный тип, а не общий RuntimeError: __main__ должен отличать его
    от поломки и уснуть до сброса, а не сыпать стектрейсами каждый цикл.
    """


class ApiBudget:
    """Счётчик вызовов одного источника к одному API за сутки UTC."""

    def __init__(self, dsn: str, source: str, limit: int,
                 api: str = "opendota") -> None:
        self._dsn = dsn
        self._db = psycopg.connect(dsn, autocommit=True)
        self._source = source
        self._limit = max(0, limit)
        self._api = api

    def close(self) -> None:
        self._db.close()

    def _today(self) -> str:
        # День UTC, а не локальный: OpenDota сбрасывает счётчик в 00:00
        # UTC, и считать по московскому календарю значило бы сбрасывать
        # бюджет на три часа раньше и получать 429 на ровном месте.
        return datetime.now(timezone.utc).date().isoformat()

    def spend(self, n: int = 1) -> int:
        """Записать вызов. Бросает BudgetExhausted, если доля выбрана.

        Инкремент и проверка — ОДНИМ запросом: два процесса одного
        источника (например, после неудачного перезапуска) иначе оба
        прочитали бы «ещё можно» и вместе перебрали лимит.
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
        if used > self._limit:
            raise BudgetExhausted(
                f"{self._source}: суточная доля {self._api} выбрана "
                f"({used}/{self._limit})")
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
    """OPENDOTA_BUDGET — доля ЭТОГО процесса в суточной квоте.

    Задаётся на коллектор в dev-recover, а не общей таблицей долей:
    процессы поднимаются независимо, у каждого свой набор переменных, и
    держать распределение там же, где остальная его конфигурация,
    честнее, чем в отдельном месте, о котором легко забыть.
    """
    return configure(dsn, source, int(os.getenv("OPENDOTA_BUDGET", "0")))
