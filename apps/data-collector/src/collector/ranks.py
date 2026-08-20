"""Кэш рангов по account_id (спринт 123).

Экономика, ради которой всё затевалось. Ранговый фильтр STRATZ (спринт
94) отбраковывает ~95% матчей, и каждая отбраковка стоит одного запроса
из 15 000 суточных. Плата за ОДНОРАЗОВОЕ знание: тот же игрок завтра
сыграет ещё десять матчей, и мы заплатим за него ещё десять раз.

Ранг — свойство игрока, а не матча, и меняется он неделями. Значит его
можно купить один раз и переиспользовать: опрошенный Immortal делает
бесплатно распознаваемыми все свои будущие матчи. Суточная квота из
расходника становится накопителем.

Второе применение, не менее важное. Своя разбивка реплеев упирается не в
CPU (3.2 с на матч), а в канал: 58 МиБ на матч, 24-50 Мбит/с, то есть
физический потолок в несколько тысяч матчей в сутки. Скачать весь поток
нельзя — надо ОТБИРАТЬ до скачивания, а отбирать нечем: у Valve ранга
нет. Кэш и есть тот фильтр.

Три состояния ранга различаются строго (см. миграцию 006):
  отсутствие строки — аккаунт не встречался;
  rank_tier IS NULL — встречался, но не опрошен;
  rank_tier = 0     — опрошен, ранга нет (закрытый профиль).
Слить последние два значило бы вечно опрашивать закрытые профили — ровно
тот механизм самоудушения, что разобран в спринте 87.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Protocol

import psycopg
import requests

from .candidates import Candidate, CandidateQueue
from .sources.steam import (ANONYMOUS_ACCOUNT_ID, SteamAPIError,
                            SteamMatchStream, match_account_stats)

logger = logging.getLogger("collector.ranks")

# Нижняя граница Immortal в шкале rank_tier (тир*10 + звезда). Та же
# шкала, что у STRATZ `rank` и у OpenDota `rank_tier` — совпадение
# проверено в спринте 94.
IMMORTAL_MIN_RANK = 80

# Ранг «спросили, ответа нет». Не NULL: NULL означает «ещё не спрашивали».
RANK_UNKNOWN = 0

# Сколько дней ранг считается свежим. Ранги ползут медленно, а квота
# дорога; месяц — компромисс, при котором обновление съедает единицы
# процентов бюджета.
DEFAULT_TTL_DAYS = 30

# Какую долю бюджета опроса резервировать под ОБНОВЛЕНИЕ уже известных.
# Без резерва обновление не случилось бы никогда: новых аккаунтов в
# потоке всегда больше, чем протухших, и очередь первого опроса вечно
# вытесняла бы очередь обновления.
STALE_SHARE = 0.2

CURSOR_NAME = "steam_seq"


# =============================================================================
# Кэш
# =============================================================================

class RankCache:
    """Таблица PlayerRanks: кто встречался, кого спросили, что ответили."""

    def __init__(self, dsn: str) -> None:
        self._db = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self._db.close()

    # -- наполнение -----------------------------------------------------------

    def see(self, account_ids: Iterable[int]) -> int:
        """Отметить встреченные аккаунты, +1 к seen_count за каждую встречу.

        Дубликаты схлопываются в дельту ЗАРАНЕЕ, в Python. Это не
        оптимизация: `INSERT ... SELECT unnest(...) ON CONFLICT` падает с
        «cannot affect row a second time», если один и тот же ключ
        встретился во входе дважды. А дважды он встречается постоянно —
        один игрок в сотне матчей одной пачки.
        """
        deltas = Counter(a for a in map(int, account_ids)
                         if a > 0 and a != ANONYMOUS_ACCOUNT_ID)
        if not deltas:
            return 0
        ids = list(deltas)
        counts = [deltas[a] for a in ids]
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO PlayerRanks (account_id, seen_count) "
                "SELECT * FROM unnest(%s::bigint[], %s::int[]) "
                "ON CONFLICT (account_id) DO UPDATE "
                "  SET seen_count = PlayerRanks.seen_count "
                "                   + EXCLUDED.seen_count, "
                "      seen_at = NOW()",
                (ids, counts))
        return len(ids)

    def save(self, resolved: dict[int, int], source: str,
             dates: dict[int, datetime] | None = None) -> int:
        """Записать ответы. Ключ есть — значит ответ ПОЛУЧЕН.

        Отсутствие аккаунта в resolved и rank_tier=0 — разные вещи: первое
        означает «спросить не удалось, вернёмся позже», второе — «спросили,
        ранга нет». Резолвер обязан соблюдать это различие, иначе сетевой
        сбой навсегда пометит живого Immortal как безрангового.

        `dates` — когда ответ был АКТУАЛЕН, не когда его записали. Нужно
        для посева из сохранённого JSON: ранг из матча трёхмесячной
        давности нельзя выдавать за сегодняшний, иначе TTL никогда его не
        обновит и кэш будет уверенно врать.

        Свежий ответ не перезаписывается старым — условие в ON CONFLICT.
        Без него посев из архива затирал бы только что полученные ранги, а
        повторный запуск посева был бы небезопасен.

        seen_count у НОВОЙ строки — ноль, а не единица по умолчанию.
        Строку здесь создаёт ОТВЕТ о ранге, а ответ не является
        наблюдением в потоке. Единица раздувала метрику «доля потока»
        фантомными встречами: посев 9698 архивных аккаунтов поднял её до
        31.73% при в разы меньшей реальной узнаваемости (миграция 007).
        """
        if not resolved:
            return 0
        ids = list(resolved)
        ranks = [int(resolved[a]) for a in ids]
        stamps = [(dates or {}).get(a) for a in ids]
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO PlayerRanks (account_id, rank_tier, source,"
                "                         checked_at, seen_count) "
                "SELECT u.a, u.r, %s, coalesce(u.t, NOW()), 0 "
                "  FROM unnest(%s::bigint[], %s::smallint[],"
                "              %s::timestamptz[]) AS u(a, r, t) "
                "ON CONFLICT (account_id) DO UPDATE "
                "  SET rank_tier = EXCLUDED.rank_tier, "
                "      source = EXCLUDED.source, "
                "      checked_at = EXCLUDED.checked_at "
                "  WHERE PlayerRanks.checked_at IS NULL "
                "     OR PlayerRanks.checked_at < EXCLUDED.checked_at",
                (source, ids, ranks, stamps))
        return len(ids)

    # -- чтение ---------------------------------------------------------------

    def ranks_of(self, account_ids: Iterable[int]) -> dict[int, int | None]:
        """account_id -> ранг. Ключа нет — аккаунт не встречался."""
        ids = sorted({int(a) for a in account_ids})
        if not ids:
            return {}
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT account_id, rank_tier FROM PlayerRanks "
                " WHERE account_id = ANY(%s::bigint[])", (ids,))
            return {int(a): (int(r) if r is not None else None)
                    for a, r in cur.fetchall()}

    def pending(self, limit: int) -> list[int]:
        """Очередь первого опроса: самые частые вперёд."""
        if limit <= 0:
            return []
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT account_id FROM PlayerRanks "
                " WHERE checked_at IS NULL "
                " ORDER BY seen_count DESC LIMIT %s", (limit,))
            return [int(r[0]) for r in cur.fetchall()]

    def stale(self, limit: int, ttl_days: int = DEFAULT_TTL_DAYS) -> list[int]:
        """Очередь обновления: самые давно проверенные вперёд."""
        if limit <= 0:
            return []
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT account_id FROM PlayerRanks "
                " WHERE checked_at IS NOT NULL "
                "   AND checked_at < NOW() - make_interval(days => %s) "
                " ORDER BY checked_at LIMIT %s", (ttl_days, limit))
            return [int(r[0]) for r in cur.fetchall()]

    def counts(self, min_rank: int = IMMORTAL_MIN_RANK) -> dict[str, int]:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT count(*),"
                "       count(*) FILTER (WHERE checked_at IS NULL),"
                "       count(*) FILTER (WHERE rank_tier = 0),"
                "       count(*) FILTER (WHERE rank_tier >= %s),"
                "       coalesce(sum(seen_count), 0),"
                "       coalesce(sum(seen_count) FILTER"
                "                (WHERE rank_tier >= %s), 0),"
                "       count(*) FILTER (WHERE seen_count = 0)"
                "  FROM PlayerRanks", (min_rank, min_rank))
            row = cur.fetchone()
        keys = ("всего", "не опрошено", "без ранга", "immortal",
                "встреч всего", "встреч immortal", "только из архива")
        return dict(zip(keys, [int(v) for v in row]))

    def bands(self) -> list[tuple[int, int]]:
        """Гистограмма по тирам: (тир, сколько аккаунтов)."""
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT rank_tier / 10 AS tier, count(*) FROM PlayerRanks "
                " WHERE rank_tier IS NOT NULL AND rank_tier > 0 "
                " GROUP BY tier ORDER BY tier")
            return [(int(t), int(n)) for t, n in cur.fetchall()]

    # -- курсор потока Valve ---------------------------------------------------

    def get_cursor(self) -> int | None:
        with self._db.cursor() as cur:
            cur.execute("SELECT cursor_value FROM CollectorCursor "
                        " WHERE source_name = %s", (CURSOR_NAME,))
            row = cur.fetchone()
        return int(row[0]) if row else None

    def set_cursor(self, seq: int) -> None:
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO CollectorCursor (source_name, cursor_value) "
                "VALUES (%s, %s) ON CONFLICT (source_name) DO UPDATE "
                "  SET cursor_value = EXCLUDED.cursor_value, updated_at = NOW()",
                (CURSOR_NAME, str(int(seq))))


# =============================================================================
# Резолверы ранга
# =============================================================================

class RankResolver(Protocol):
    name: str

    def resolve(self, account_ids: list[int]) -> dict[int, int]:
        """account_id -> ранг (0 = ответ получен, ранга нет).

        Аккаунты, по которым ответа НЕ получено, в результат не попадают.
        """


class OpenDotaRankResolver:
    """Ранг из /players/{account_id}. Проверенный, но медленный путь.

    Один аккаунт — один запрос из ~2000 суточных. Держим его основным не
    из-за скорости, а из-за определённости: эндпоинт публичный и
    документированный, в отличие от игроцкого запроса STRATZ, чья
    доступность на нашем токене — открытый вопрос (см. StratzRankResolver).
    """

    name = "opendota"

    def __init__(self, base_url: str = "https://api.opendota.com/api",
                 timeout: float = 30.0, api_delay_s: float = 1.1,
                 api_key: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._api_delay_s = api_delay_s
        self._api_key = api_key
        self._session = requests.Session()
        # Почему аккаунт не резолвнулся. Первый живой прогон дал
        # «спрошено 200, получено 76», и по такому отчёту нельзя понять,
        # исчерпана квота или профили закрыты, — а это разные решения.
        self.failures: Counter = Counter()

    def resolve(self, account_ids: list[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        for i, aid in enumerate(account_ids):
            if i:
                time.sleep(self._api_delay_s)
            params = {"api_key": self._api_key} if self._api_key else {}
            try:
                resp = self._session.get(f"{self._base}/players/{aid}",
                                         params=params, timeout=self._timeout)
                if resp.status_code != 200:
                    self.failures[f"HTTP {resp.status_code}"] += 1
                    logger.debug("opendota %s: HTTP %s", aid, resp.status_code)
                    continue
                body = resp.json() or {}
            except (requests.RequestException, ValueError) as exc:
                self.failures[type(exc).__name__] += 1
                logger.debug("opendota %s: %s", aid, exc)
                continue
            # Ответ получен — значит запись обязана появиться, даже если
            # ранга в ней нет: закрытый профиль это ОТВЕТ, а не сбой, и
            # переспрашивать его завтра нечего.
            out[aid] = _rank_value(body.get("rank_tier"))
        return out


# Поле ранга игрока в схеме STRATZ. Вынесено в константу с env-переопре-
# делением намеренно: имя поля — единственное, чего мы не смогли
# проверить локально, а ошибка в нём валит весь запрос целиком. Правка на
# живой машине должна стоить одной переменной окружения, а не цикла
# «правка — коммит — pull».
STRATZ_RANK_FIELD = os.getenv("STRATZ_RANK_FIELD", "seasonRank")

STRATZ_API_URL = "https://api.stratz.com/graphql"
STRATZ_UA = {"User-Agent": "STRATZ_API"}


class StratzRankError(RuntimeError):
    """Ошибка запроса ранга у STRATZ."""


class StratzAuthRefused(StratzRankError):
    """Токен не принят: 401 или 403 (спринт 155).

    Отдельный класс, потому что это отказ ДРУГОГО рода. Квота лечится
    временем, сеть — повтором, а недействительный токен не лечится ничем
    в пределах прогона: каждый следующий запрос получит тот же ответ.

    Замер на VPS 2026-08-20: `ranks fill` спросил 200 аккаунтов сорока
    пакетами и сорок раз подряд получил HTTP 403 — токен протух ещё
    5 августа. Ушло на это две минуты, в лог легло сорок одинаковых
    предупреждений, а очередь кандидатов осталась пустой.

    Рассуждение для квоты в этом же файле написано верно: «остальные
    куски получат ровно тот же отказ, каждый ценой очередных
    отступлений». К отказу в доступе его просто не применили.
    """


class StratzQuotaExhausted(StratzRankError):
    """Кончилась ЧАСОВАЯ или суточная квота — отступать бессмысленно.

    От всплеска отличается принципиально. Всплеск лечится паузой в
    секунды; исчерпанная квота часа не лечится ничем, кроме следующего
    часа, и отступление 2/4/8/16 против неё — способ потратить полминуты
    на запрос и не получить ничего. Живой прогон 2026-08-06: после
    исчерпания квоты цикл ушёл в часы бессмысленных попыток, потому что
    этой разницы код не знал.
    """


# Часовой лимит Default-токена STRATZ. Именно он СВЯЗЫВАЮЩИЙ: 8/с и
# 150/мин позволяют куда больший темп, но 1500/час режет всё до одного
# запроса в 2.4 секунды. Паузу надо считать по самому строгому лимиту, а
# не по самому заметному — ровно эту ошибку я и допустил, взяв 8/с и
# поставив 0.2 с, то есть 18000 запросов в час.
STRATZ_HOUR_LIMIT = 1500
STRATZ_MIN_DELAY_S = 3600.0 / STRATZ_HOUR_LIMIT   # 2.4 с


def is_admin_only(message: str) -> bool:
    """Отказ «эндпоинт только для админов» — не сетевой сбой, а приговор."""
    return "not an admin" in message.lower()


# «You have surpassed the maximum take value of : 5» — STRATZ сообщает
# предел пачки прямо в тексте ошибки. Не захардкожен: предел у них уже
# отличается между запросами (у матчей он другой), и вычитать его из
# ответа надёжнее, чем держать в константе, которая протухнет молча.
_TAKE_LIMIT_RE = re.compile(r"maximum take value of\s*:?\s*(\d+)")


def take_limit(message: str) -> int | None:
    """Предел размера пачки из текста ошибки STRATZ; None — не про это."""
    m = _TAKE_LIMIT_RE.search(message)
    return int(m.group(1)) if m else None


class _BatchShrunk(Exception):
    """Внутренний сигнал: пачка ужата, тот же кусок надо повторить."""


class StratzRankResolver:
    """Ранг из player/players STRATZ.

    Пакетный `players(steamAccountIds:)` — множитель квоты: она считается
    по HTTP-запросам, а не по игрокам. Ровно такой же пакетный
    `matches(ids:)` оказался админским (спринты 121/122), поэтому пакет
    здесь НЕ предполагается, а ПРОВЕРЯЕТСЯ: отказ «User is not an admin»
    навсегда переводит резолвер на одиночные запросы.

    Живая проверка 2026-08-06 дала третий, неожиданный исход: пакет
    разрешён, но ограничен ПЯТЬЮ id — «You have surpassed the maximum
    take value of : 5». Предел вычитывается из текста ошибки и пачка
    ужимается на лету: держать его константой значило бы протухнуть
    молча в день, когда STRATZ его изменит.
    """

    name = "stratz"

    # Предел пачки, измеренный на живом токене. Значение по умолчанию, а
    # не истина: настоящий предел приходит из ответа сервера.
    DEFAULT_BATCH = 5

    def __init__(self, token: str, api_url: str = STRATZ_API_URL,
                 timeout: float = 30.0, batch_size: int = DEFAULT_BATCH,
                 rank_field: str | None = None,
                 api_delay_s: float = STRATZ_MIN_DELAY_S,
                 retries: int = 4, backoff_base_s: float = 2.0,
                 run_budget: int | None = None) -> None:
        if not token:
            raise ValueError("stratz: пустой токен")
        self._token = token
        self._url = api_url
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        # Темп по САМОМУ СТРОГОМУ лимиту (1500/час), а не по 8/с.
        self._api_delay_s = api_delay_s
        # Бюджет запросов на прогон: квота часа общая с коллектором
        # матчей, выесть её целиком значит остановить сбор.
        self._run_budget = (run_budget if run_budget is not None
                            else int(os.getenv("STRATZ_RUN_BUDGET", "1000")))
        self.requests = 0
        # Остаток по заголовкам ответа; None — сервер не сказал.
        self.remaining_hour: int | None = None
        self.remaining_day: int | None = None
        self._retries = max(0, retries)
        self._backoff_base_s = backoff_base_s
        self._last_call = 0.0
        self._field = rank_field or STRATZ_RANK_FIELD
        # None — не проверяли, True — пакет работает, False — админский.
        # Живая проверка 2026-08-06: пакет РАЗРЕШЁН на нашем токене, в
        # отличие от matches(ids:). Автоопределение оставлено — доступ
        # уже менялся, и молча упасть на этом второй раз мы не должны.
        self.batch_allowed: bool | None = None
        self.failures: Counter = Counter()
        self._session = requests.Session()

    # -- транспорт -------------------------------------------------------------

    def _gql(self, query: str, variables: dict) -> dict:
        if self.requests >= self._run_budget:
            raise StratzQuotaExhausted(
                f"бюджет прогона исчерпан ({self._run_budget} запросов)")
        for attempt in range(self._retries + 1):
            gap = self._api_delay_s - (time.monotonic() - self._last_call)
            if gap > 0:
                time.sleep(gap)
            resp = self._session.post(
                self._url, json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {self._token}", **STRATZ_UA},
                timeout=self._timeout)
            self._last_call = time.monotonic()
            self.requests += 1
            self._read_limits(resp)
            if resp.status_code != 429:
                break
            # 429 при нулевом остатке часа — это НЕ всплеск. Отступать
            # незачем: до конца часа ответа не будет, и каждая попытка
            # лишь тратит полминуты и ещё один запрос из суточной квоты.
            if self.remaining_hour == 0 or self.remaining_day == 0:
                raise StratzQuotaExhausted(
                    f"квота исчерпана (час: {self.remaining_hour}, "
                    f"сутки: {self.remaining_day})")
            if attempt == self._retries:
                # Заголовков нет, но 429 не ушёл за все отступления —
                # почти наверняка та же исчерпанная квота, а не всплеск.
                raise StratzQuotaExhausted(
                    f"HTTP 429 не ушёл за {self._retries} отступлений")
            wait = self._backoff_base_s * (2 ** attempt)
            logger.info("STRATZ 429 — ждём %.0f с (попытка %d/%d)",
                        wait, attempt + 1, self._retries)
            time.sleep(wait)
        # Отказ в доступе проверяем ДО разбора тела: 401/403 приходят и
        # без JSON, и с ним, а значат одно и то же в обоих случаях.
        if resp.status_code in (401, 403):
            raise StratzAuthRefused(
                f"HTTP {resp.status_code}: STRATZ не принимает токен")
        body = resp.json() if resp.content else {}
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            raise StratzRankError(str(errors[0].get("message", errors)))
        if resp.status_code != 200:
            raise StratzRankError(f"HTTP {resp.status_code}")
        return (body or {}).get("data") or {}

    # -- запросы ---------------------------------------------------------------

    def _read_limits(self, resp) -> None:
        """Остатки квот из заголовков ответа STRATZ."""
        for header, attr in (("x-ratelimit-remaining-hour", "remaining_hour"),
                             ("x-ratelimit-remaining-day", "remaining_day")):
            raw = resp.headers.get(header)
            if raw is None:
                continue
            try:
                setattr(self, attr, int(raw))
            except (TypeError, ValueError):
                pass

    def _batch_query(self) -> str:
        return ("query($ids: [Long!]!) { players(steamAccountIds: $ids) "
                "{ steamAccount { id %s } } }" % self._field)

    def _single_query(self) -> str:
        return ("query($id: Long!) { player(steamAccountId: $id) "
                "{ steamAccount { id %s } } }" % self._field)

    def _try_batch(self, ids: list[int]) -> dict[int, int] | None:
        """Пакетный запрос; None — пакет недоступен, дальше по одному."""
        if self.batch_allowed is False:
            return None
        try:
            data = self._gql(self._batch_query(), {"ids": ids})
        except StratzRankError as exc:
            if is_admin_only(str(exc)):
                self.batch_allowed = False
                logger.warning(
                    "STRATZ: пакетный players(steamAccountIds:) закрыт "
                    "(%s) — переходим на одиночные запросы", exc)
                return None
            limit = take_limit(str(exc))
            if limit and limit < len(ids):
                self._batch_size = limit
                logger.warning("STRATZ: предел пачки %d — ужимаемся "
                               "и повторяем тот же кусок", limit)
                raise _BatchShrunk from exc
            raise
        self.batch_allowed = True
        out: dict[int, int] = {}
        for rec in data.get("players") or []:
            aid, rank = _stratz_account(rec, self._field)
            if aid is not None:
                out[aid] = rank
        # Игрок, которого STRATZ не знает, в ответе не появится вовсе.
        # Это ОТВЕТ («ранга нет»), а не сбой: пакет доехал целиком.
        for aid in ids:
            out.setdefault(aid, RANK_UNKNOWN)
        return out

    def resolve(self, account_ids: list[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        start = 0
        while start < len(account_ids):
            chunk = account_ids[start:start + self._batch_size]
            before = self._batch_size
            try:
                batched = self._try_batch(chunk)
            except _BatchShrunk:
                # Повторяем тот же кусок — но ТОЛЬКО если пачка правда
                # уменьшилась. Без этой проверки сервер, повторяющий один
                # и тот же предел, вешал бы процесс намертво: обнаружено
                # мутационным тестом, а не в проде.
                if self._batch_size < before:
                    continue
                self.failures["предел пачки не уменьшился"] += len(chunk)
                start += len(chunk)
                continue
            except StratzAuthRefused as exc:
                # Как и квота, не лечится продолжением — но по другой
                # причине: дело не в лимите, а в самом токене. Отдаём
                # уже полученное и уходим, не тратя ещё сорок запросов
                # на тот же ответ.
                self.failures["токен не принят"] += len(account_ids) - start
                raise StratzAuthRefused(str(exc)) from None
            except StratzQuotaExhausted:
                # Квота не лечится продолжением: остальные куски получат
                # ровно тот же отказ, каждый ценой очередных отступлений.
                # Отдаём то, что успели, — записи уже полученных рангов
                # терять нельзя.
                self.failures["квота исчерпана"] += len(account_ids) - start
                raise StratzQuotaExhausted(str(self.failures)) from None
            except StratzRankError as exc:
                # НЕ откатываемся на одиночные: сбой пачки из-за квоты
                # или сети превратился бы в полсотни запросов вместо
                # одного и добил бы лимит окончательно. Ровно это и
                # случилось в первом живом прогоне. Одиночные — только
                # когда пакет закрыт админски, то есть навсегда.
                self.failures[str(exc)] += len(chunk)
                logger.warning("STRATZ пакет %d id: %s", len(chunk), exc)
                start += len(chunk)
                continue
            start += len(chunk)
            if batched is not None:
                out.update(batched)
                continue
            for aid in chunk:
                try:
                    data = self._gql(self._single_query(), {"id": aid})
                except StratzQuotaExhausted:
                    raise
                except StratzRankError as exc:
                    self.failures[str(exc)] += 1
                    logger.debug("stratz %s: %s", aid, exc)
                    continue
                rec = (data.get("player") or {})
                _, rank = _stratz_account(rec, self._field)
                out[aid] = rank
        return out


def _stratz_account(rec: dict, field: str) -> tuple[int | None, int]:
    account = (rec or {}).get("steamAccount") or {}
    raw_id = account.get("id")
    try:
        aid = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        aid = None
    return aid, _rank_value(account.get(field))


def _rank_value(raw) -> int:
    """Привести ранг к целому; всё непонятное — RANK_UNKNOWN."""
    if raw is None or isinstance(raw, bool):
        return RANK_UNKNOWN
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return RANK_UNKNOWN
    return value if value > 0 else RANK_UNKNOWN


# =============================================================================
# Решение по матчу
# =============================================================================

# Ранкед-матчмейкинг и All Pick — те же значения, что фильтрует
# opendota_public: турбо и прочие режимы ломают экономические
# закономерности, на которых стоит модель.
LOBBY_RANKED = 7
GAME_MODE_ALL_PICK = 22
MIN_DURATION_S = 900

# Порог «сколько игроков матча должны иметь известный ранг». ДВА, а не
# четыре: замер на 5000 матчей потока дал 3.8 видимых аккаунта из десяти
# (62% профилей скрыты), и требование четырёх известных недостижимо для
# половины матчей в принципе. Опираться на двух не так шатко, как
# кажется: матчмейкинг Dota однороден по рангу, и два подтверждённых
# Immortal — сильное свидетельство, что иммортален весь матч.
MIN_KNOWN_RANKS = 2
MIN_IMMORTAL_SHARE = 0.6


def stream_filter(match: dict, min_duration_s: int = MIN_DURATION_S
                  ) -> tuple[bool, str]:
    """Дешёвый отсев ДО обращения к кэшу: режим и длительность.

    Считается по полям, которые Valve уже прислал, поэтому стоит ноль.
    Порядок важен: сначала выбрасываем турбо и брошенные матчи, и только
    потом смотрим ранги — иначе воронка отчёта смешает «не наш режим» с
    «низкий ранг», а это разные болезни с разным лечением.
    """
    if match.get("lobby_type") != LOBBY_RANKED:
        return False, "не ранкед"
    if match.get("game_mode") != GAME_MODE_ALL_PICK:
        return False, "не all pick"
    if int(match.get("duration") or 0) < min_duration_s:
        return False, "короткий"
    return True, "годен"


def classify_match(ranks: dict[int, int | None], min_rank: int = IMMORTAL_MIN_RANK,
                   min_known: int = MIN_KNOWN_RANKS,
                   min_share: float = MIN_IMMORTAL_SHARE) -> tuple[bool, str]:
    """Брать ли матч, зная ранги его игроков.

    Требовать все десять известных рангов нельзя: 62% профилей скрыты
    навсегда, и такой матч не станет пригодным никогда. Поэтому решение
    принимается по ДОЛЕ среди известных, при минимуме известных — иначе
    один случайный Immortal из одного известного игрока протащил бы
    любой матч.

    Отсутствие ранга — НЕ свидетельство низкого ранга. Кэш заполняется
    сверху вниз (посев из архива дал именно Immortal), поэтому «нет в
    кэше» означает «ещё не спрашивали», и трактовать это как отказ
    значило бы отбраковывать по собственному незнанию.

    Возвращает (брать, причина). Причина — для посуточной статистики
    циклов: молчаливый отказ мы уже проходили, он стоил недели.
    """
    known = [r for r in ranks.values() if r is not None and r > 0]
    if len(known) < min_known:
        return False, "мало известных рангов"
    high = sum(1 for r in known if r >= min_rank)
    share = high / len(known)
    if share < min_share:
        return False, "низкий ранг"
    return True, "берём"


# =============================================================================
# Операции
# =============================================================================

def seed(cache: RankCache, stream: SteamMatchStream, matches: int,
         batch: int = 100) -> dict[str, int]:
    """Пройти по потоку Valve и запомнить встреченные account_id."""
    seq = cache.get_cursor()
    if seq is None:
        logger.info("курсор пуст — ищем хвост последовательности")
        seq = stream.tip_seq()
        logger.info("хвост последовательности: %s", seq)
    stats = {"матчей": 0, "аккаунтов": 0, "вызовов": 0,
             "слотов": 0, "скрытых": 0}
    seen_batch: list[int] = []
    while stats["матчей"] < matches:
        try:
            got = stream.batch(seq, count=min(batch, matches - stats["матчей"]))
        except SteamAPIError as exc:
            logger.warning("поток Valve: %s", exc)
            break
        stats["вызовов"] += 1
        if not got:
            # Догнали живой край потока: матчей дальше пока нет.
            logger.info("догнали край потока на seq %s", seq)
            break
        for m in got:
            accounts, slots, hidden = match_account_stats(m)
            seen_batch.extend(accounts)
            stats["слотов"] += slots
            stats["скрытых"] += hidden
            seq = max(seq, int(m.get("match_seq_num") or seq) + 1)
        stats["матчей"] += len(got)
    stats["аккаунтов"] = cache.see(seen_batch)
    cache.set_cursor(seq)
    return stats


def visible_per_match(stats: dict[str, int]) -> float:
    """Сколько игроков матча видны по account_id. Ключевое для отбора."""
    return (stats["слотов"] - stats["скрытых"]) / max(stats["матчей"], 1)


# Сетка порогов для развёртки. Выбирать порог на глаз мы уже пробовали
# (min_known=4 оказался недостижим), поэтому scan печатает ВСЮ сетку и
# решение принимается по числам.
SWEEP_KNOWN = (2, 3, 4, 5)
SWEEP_SHARE = (0.5, 0.6, 0.75, 1.0)

# Отставание от живого края, при котором сканер прыгает вперёд.
#
# Зачем вообще прыгать. Мировой поток — порядка миллиона матчей в сутки,
# а Valve отдаёт нам около 2400 за проход и упирается в 429. Читая
# ПОДРЯД, сканер отстаёт примерно на две трети скорости потока, то есть
# курсор уезжает назад на ~0.7 дня за каждый день работы. Реплеи Valve
# хранит около двух недель — значит недели через три кандидаты начнут
# массово помечаться expired, и приток тихо сойдёт на нет. Ничего не
# сломается, просто перестанет работать: ровно та форма отказа, которая
# нам уже стоила недель.
#
# Читать подряд нам и не нужно. Нужны не ВСЕ матчи, а полторы тысячи
# подходящих в сутки, и распределены они по потоку равномерно. Поэтому
# сканер работает как выборка, а не как перечисление, и это осознанный
# отказ от полноты охвата.
SCAN_MAX_LAG_S = 6 * 3600
SCAN_TARGET_LAG_S = 3600

# Прыгаем на 80% расчётного расстояния. Недолёт самоисправляется
# следующим проходом, перелёт стоит целого прохода впустую — асимметрия
# в пользу осторожности.
JUMP_SAFETY = 0.8

# Запасная оценка темпа потока (номеров последовательности в секунду),
# если измерить не удалось. Из замера ~789 match_id/мин.
DEFAULT_SEQ_RATE = 13.0


def stream_rate(matches: list[dict]) -> float | None:
    """Номеров последовательности в секунду реального времени.

    Измеряется по самому же прочитанному куску — ни одного лишнего
    вызова API. Шум от разной длительности матчей (номер выдаётся в
    конце, start_time — начало) на паре тысяч матчей усредняется.
    """
    stamps = [(int(m.get("match_seq_num") or 0), int(m.get("start_time") or 0))
              for m in matches]
    stamps = [(s, t) for s, t in stamps if s > 0 and t > 0]
    if len(stamps) < 2:
        return None
    stamps.sort()
    d_seq = stamps[-1][0] - stamps[0][0]
    d_time = stamps[-1][1] - stamps[0][1]
    if d_seq <= 0 or d_time <= 0:
        return None
    return d_seq / d_time


def lag_seconds(matches: list[dict], now: float) -> float | None:
    """На сколько мы отстаём от живого края, по времени начала матчей."""
    stamps = [int(m.get("start_time") or 0) for m in matches]
    stamps = [t for t in stamps if t > 0]
    if not stamps:
        return None
    return max(now - max(stamps), 0.0)


def _verify_forward(stream: SteamMatchStream, base: int, jump: int,
                    tries: int = 4) -> int:
    """Прыжок вперёд, ПРОВЕРЕННЫЙ на наличие матчей.

    Без проверки перелёт за живой край оставлял бы курсор в пустоте
    навсегда: каждый следующий проход читал бы ноль матчей и не имел бы
    ни малейшего повода вернуться. Стоит это 1-4 вызова.
    """
    while jump > 0 and tries > 0:
        try:
            if stream.batch(base + jump, count=1):
                return base + jump
        except SteamAPIError as exc:
            logger.debug("прыжок на %s не удался: %s", base + jump, exc)
        jump //= 2
        tries -= 1
    return base


def rank_summary(ranks: dict[int, int | None]) -> tuple[int, int, int]:
    """(известных рангов, из них immortal, средний известный ранг).

    Сохраняется вместе с кандидатом ради проверки ТОЧНОСТИ правила: оно
    берёт матч по двум известным рангам из десяти игроков, и это
    допущение. Без этих чисел проверить его будет нечем.
    """
    known = [r for r in ranks.values() if r is not None and r > 0]
    if not known:
        return 0, 0, 0
    high = sum(1 for r in known if r >= IMMORTAL_MIN_RANK)
    return len(known), high, int(sum(known) / len(known))


def scan(cache: RankCache, stream: SteamMatchStream, matches: int,
         batch: int = 100, min_duration_s: int = MIN_DURATION_S,
         queue=None) -> tuple[dict[str, int], dict[tuple[int, float], int]]:
    """Пройти по потоку Valve и посчитать, сколько матчей мы бы взяли.

    Это замер, а не сбор: ничего не скачивается. Он отвечает на
    единственный вопрос, от которого зависит вся своя разбивка — сколько
    матчей в сутки кэш способен отобрать. Заодно пополняет кэш
    встреченными аккаунтами: проход по потоку всё равно сделан, не
    воспользоваться им было бы расточительно.

    Ранги запрашиваются ОДНИМ запросом на пачку матчей, а не на матч:
    четыреста аккаунтов в одном IN дешевле сотни round-trip'ов.
    """
    seq = cache.get_cursor()
    if seq is None:
        seq = stream.tip_seq()
    funnel: dict[str, int] = {}
    sweep: dict[tuple[int, float], int] = {
        (k, s): 0 for k in SWEEP_KNOWN for s in SWEEP_SHARE}
    seen_all: list[int] = []
    seen_matches: list[dict] = []
    done = 0
    while done < matches:
        try:
            got = stream.batch(seq, count=min(batch, matches - done))
        except SteamAPIError as exc:
            logger.warning("поток Valve: %s", exc)
            break
        if not got:
            break
        done += len(got)
        seen_matches.extend(got)

        prepared = []
        wanted: set[int] = set()
        for m in got:
            seq = max(seq, int(m.get("match_seq_num") or seq) + 1)
            accounts, _, _ = match_account_stats(m)
            seen_all.extend(accounts)
            ok, why = stream_filter(m, min_duration_s)
            if not ok:
                funnel[why] = funnel.get(why, 0) + 1
                continue
            prepared.append((m, accounts))
            wanted.update(accounts)

        known = cache.ranks_of(wanted) if wanted else {}
        chosen = []
        for m, accounts in prepared:
            ranks = {a: known.get(a) for a in accounts}
            take, why = classify_match(ranks)
            funnel[why] = funnel.get(why, 0) + 1
            if take and queue is not None:
                n_known, n_high, avg = rank_summary(ranks)
                chosen.append(Candidate(
                    match_id=int(m.get("match_id") or 0),
                    match_seq_num=int(m.get("match_seq_num") or 0),
                    started_at=m.get("start_time"),
                    known_ranks=n_known, immortal_ranks=n_high,
                    avg_known_rank=avg))
            for k in SWEEP_KNOWN:
                for s in SWEEP_SHARE:
                    if classify_match(ranks, min_known=k, min_share=s)[0]:
                        sweep[(k, s)] += 1
        if chosen:
            funnel["в очередь"] = funnel.get("в очередь", 0) + queue.add(chosen)

    funnel["всего матчей"] = done
    cache.see(seen_all)

    # Догон живого края. Считается по данным, которые уже в руках:
    # start_time каждого матча даёт и отставание, и темп потока — ни
    # одного лишнего вызова API.
    lag = lag_seconds(seen_matches, time.time())
    if lag is not None:
        funnel["отставание, ч"] = int(lag / 3600)
    if lag is not None and lag > SCAN_MAX_LAG_S:
        rate = stream_rate(seen_matches) or DEFAULT_SEQ_RATE
        jump = int((lag - SCAN_TARGET_LAG_S) * rate * JUMP_SAFETY)
        if jump > 0:
            jumped = _verify_forward(stream, seq, jump)
            if jumped > seq:
                logger.info("отставание %.1f ч — прыгаем вперёд на %d "
                            "(темп %.1f seq/с)", lag / 3600, jumped - seq, rate)
                funnel["прыжок вперёд"] = jumped - seq
                seq = jumped
            else:
                logger.warning("отставание %.1f ч, но прыжок не подтвердился",
                               lag / 3600)
    cache.set_cursor(seq)
    return funnel, sweep


def sweep_table(sweep: dict[tuple[int, float], int], total: int) -> str:
    """Развёртка порогов: строки — известных рангов, столбцы — доля."""
    head = "известных \\ доля  " + "".join(f"{s:>8.0%}" for s in SWEEP_SHARE)
    lines = [head]
    for k in SWEEP_KNOWN:
        cells = "".join(
            f"{100.0 * sweep[(k, s)] / max(total, 1):>7.2f}%"
            for s in SWEEP_SHARE)
        lines.append(f"{k:>16}  {cells}")
    return "\n".join(lines)


def rawstore_pairs(match: dict) -> list[tuple[int, int, datetime]]:
    """(account_id, ранг, момент актуальности) из сохранённого JSON матча.

    В сыром JSON OpenDota ранг лежит У КАЖДОГО ИГРОКА (`rank_tier`), а не
    средним по матчу — то есть каждый сохранённый матч это до десяти пар
    «аккаунт → ранг», уже скачанных и уже оплаченных квотой.

    Матч без `start_time` пропускается целиком: ранг без даты пришлось бы
    выдать за сегодняшний, а это ровно та ложь, из-за которой TTL
    перестанет обновлять запись. Лучше не знать, чем знать неверно.

    Игроки без ранга (закрытый профиль на момент матча) НЕ записываются:
    трёхмесячной давности «ранга нет» почти ничего не говорит, зато забило
    бы очередь обновления мусором.
    """
    started = match.get("start_time")
    if not started:
        return []
    try:
        when = datetime.fromtimestamp(int(started), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return []
    out = []
    for p in match.get("players") or []:
        rank = _rank_value(p.get("rank_tier"))
        if rank <= 0:
            continue
        try:
            aid = int(p.get("account_id") or 0)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid == ANONYMOUS_ACCOUNT_ID:
            continue
        out.append((aid, rank, when))
    return out


def harvest_rawstore(cache: RankCache, store, limit: int | None = None,
                     chunk: int = 500) -> dict[str, int]:
    """Посеять кэш из сохранённого JSON. Ноль вызовов внешних API.

    Для одного аккаунта в архиве может быть несколько матчей с разными
    рангами — берём САМЫЙ СВЕЖИЙ. Иначе порядок обхода бакета решал бы,
    какой ранг победит, и результат посева зависел бы от того, в каком
    порядке MinIO вернул объекты.
    """
    best: dict[int, tuple[datetime, int]] = {}
    stats = {"матчей": 0, "без даты": 0, "пар": 0, "аккаунтов": 0,
             "immortal": 0}
    for n, mid in enumerate(store.iter_match_ids()):
        if limit is not None and n >= limit:
            break
        match = store.get(mid)
        if not match:
            continue
        stats["матчей"] += 1
        pairs = rawstore_pairs(match)
        if not pairs and match.get("players"):
            stats["без даты"] += 1
        for aid, rank, when in pairs:
            stats["пар"] += 1
            known = best.get(aid)
            if known is None or when > known[0]:
                best[aid] = (when, rank)

    ids = list(best)
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        cache.save({a: best[a][1] for a in part}, "opendota-json",
                   dates={a: best[a][0] for a in part})
    stats["аккаунтов"] = len(ids)
    stats["immortal"] = sum(1 for _, r in best.values()
                            if r >= IMMORTAL_MIN_RANK)
    return stats


def fill(cache: RankCache, resolver: RankResolver, budget: int,
         ttl_days: int = DEFAULT_TTL_DAYS, chunk: int = 50,
         stale_share: float = STALE_SHARE) -> dict[str, int]:
    """Опросить очередь: сначала новые и частые, затем протухшие."""
    if budget <= 0:
        return {"спрошено": 0, "получено": 0, "immortal": 0}
    stale = cache.stale(int(budget * stale_share), ttl_days)
    pending = cache.pending(budget - len(stale))
    # Недобор в очереди новых отдаём обновлению: бюджет — это квота
    # внешнего API, и недотратить её так же плохо, как перетратить.
    # Перезапрашиваем целиком, а не дописываем хвост: срез уже выданного
    # списка не совпал бы с новым LIMIT и молча терял бы аккаунты.
    if len(pending) + len(stale) < budget:
        stale = cache.stale(budget - len(pending), ttl_days)
    todo = list(dict.fromkeys(pending + stale))[:budget]
    # «спрошено» — сколько аккаунтов РЕАЛЬНО отправлено резолверу, а не
    # сколько запланировано. Прогон 2026-08-06 напечатал «спрошено 5000,
    # получено 0» после остановки на первом же куске, и это читалось как
    # пять тысяч потраченных впустую запросов вместо пятидесяти.
    stats = {"в очереди": len(todo), "спрошено": 0, "получено": 0,
             "immortal": 0}
    for start in range(0, len(todo), chunk):
        part = todo[start:start + chunk]
        try:
            resolved = resolver.resolve(part)
            stats["спрошено"] += len(part)
        except StratzQuotaExhausted as exc:
            stats["спрошено"] += len(part)
            # Прогон закончился не по нашей воле — но закончился ЧЕСТНО:
            # то, что успели опросить, уже записано, а остаток очереди
            # никуда не денется до следующего часа.
            logger.warning("STRATZ: %s — прогон остановлен", exc)
            stats["остановлен"] = 1
            stats["  причина: квота"] = 1
            break
        if not resolved:
            continue
        cache.save(resolved, resolver.name)
        stats["получено"] += len(resolved)
        stats["immortal"] += sum(1 for r in resolved.values()
                                 if r >= IMMORTAL_MIN_RANK)
    # Причины молчания — в тот же отчёт. Разница между «квота кончилась»
    # и «профили закрыты» это разные решения, а по одному числу
    # «получено» их не различить.
    for reason, n in getattr(resolver, "failures", Counter()).most_common(5):
        stats[f"  не ответил {reason}"] = n
    return stats


def queue_report(queue) -> str:
    """Текст отчёта по очереди кандидатов.

    Отдельная функция, а не печать внутри CLI, по конкретной причине.
    Спринт 129 добавлял сюда блок точности скриптовой правкой по шаблону
    с неверным отступом: str.replace молча ничего не сделал, тесты
    покрывали precision() на уровне БД и ни одного — вывод команды, и
    главная поставка спринта уехала в main отсутствующей. Пока отчёт
    формируется чистой функцией, такое ловится тестом.
    """
    lines = ["=== очередь кандидатов ==="]
    for state, n in queue.stats().items():
        lines.append(f"{state:>22}: {n}")

    prec = queue.precision()
    if prec["скачано"]:
        lines += ["", "=== точность правила отбора ==="]
        # Ширина по самому длинному ключу, а не константой: имена метрик
        # длиннее колонки состояний, и при жёстких 22 колонка разъезжается.
        w = max(22, max(len(k) for k in prec))
        for k, v in prec.items():
            lines.append(f"{k:>{w}}: {v}")
        if prec["факт известен"]:
            known = prec["факт известен"]
            lines.append(
                f"{'ТОЧНОСТЬ':>{w}}: {prec['доля immortal, %']}% игроков в"
                " отобранных матчах — immortal")
            lines.append(
                f"{'МУСОР':>{w}}: "
                f"{100.0 * prec['мусор (immortal < половины)'] / known:.1f}%"
                " матчей, где immortal меньше половины")
        else:
            lines.append("  факт ещё не собран: эти матчи скачаны до"
                         " миграции 009")

    sample = queue.precision_sample()
    if sample:
        lines += ["", "последние скачанные (match_id, известных рангов,"
                      " средний ранг) — это ПРЕДСКАЗАНИЕ кэша, не факт:"]
        for mid, known, avg in sample:
            lines.append(f"  {mid}  known={known}  avg_rank={avg}")
    return "\n".join(lines)


def report(cache: RankCache, min_rank: int = IMMORTAL_MIN_RANK) -> str:
    c = cache.counts(min_rank)
    lines = ["=== кэш рангов ==="]
    for key in ("всего", "не опрошено", "без ранга", "immortal",
                "только из архива"):
        lines.append(f"{key:>16}: {c[key]}")
    asked = c["всего"] - c["не опрошено"]
    if asked:
        lines.append(f"{'доля immortal':>16}: "
                     f"{100.0 * c['immortal'] / asked:.1f}% от опрошенных "
                     f"(смещено: посев из архива — это заведомо Immortal)")
    # Какую долю ПОТОКА закрывают известные Immortal. Считается только по
    # встречам в потоке Valve: у строк, созданных ответом о ранге,
    # seen_count = 0, иначе посев из архива раздувал бы числитель
    # фантомными встречами (миграция 007).
    if c["встреч всего"]:
        lines.append(f"{'доля потока':>16}: "
                     f"{100.0 * c['встреч immortal'] / c['встреч всего']:.2f}% "
                     f"встреч в потоке Valve — известные immortal")
    lines.append("Настоящий ответ про отбор даёт make ranks-scan: "
                 "воронка по матчам, а не по игрокам.")
    bands = cache.bands()
    if bands:
        lines.append("тиры: " + "  ".join(f"{t}:{n}" for t, n in bands))
    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def probe_stratz(token: str) -> str:
    """Выяснить, что именно доступно нашему токену, не гадая.

    Спринт 121 стоил дня из-за предположения, что раз поле есть в схеме,
    оно и работает. Здесь порядок обратный: сперва интроспекция полей
    SteamAccountType, затем реальные запросы, и только потом выводы.
    """
    r = StratzRankResolver(token)
    out = ["=== зонд STRATZ ==="]
    introspect = ("{ __type(name: \"SteamAccountType\") "
                  "{ fields { name } } }")
    try:
        data = r._gql(introspect, {})
        names = [f["name"] for f in
                 ((data.get("__type") or {}).get("fields") or [])]
        hits = [n for n in names if "rank" in n.lower()]
        out.append(f"полей у SteamAccountType: {len(names)}")
        out.append(f"похожих на ранг: {hits}")
    except StratzRankError as exc:
        out.append(f"интроспекция не удалась: {exc}")

    probe_id = 34505203  # публичный аккаунт, годится как мишень
    for label, query, variables in (
            ("одиночный player", r._single_query(), {"id": probe_id}),
            ("пакетный players", r._batch_query(), {"ids": [probe_id]})):
        try:
            data = r._gql(query, variables)
            out.append(f"{label}: OK -> {data}")
        except StratzRankError as exc:
            verdict = "АДМИНСКИЙ" if is_admin_only(str(exc)) else "ошибка"
            out.append(f"{label}: {verdict} -> {exc}")
    return "\n".join(out)


def build_resolver(name: str | None = None) -> RankResolver:
    """Выбрать резолвер. По умолчанию — STRATZ, если есть токен.

    Причина умолчания измерена: квота STRATZ считается по HTTP-запросам,
    а пакетный players(steamAccountIds:) на нашем токене РАЗРЕШЁН (зонд
    2026-08-06), то есть один запрос закрывает полсотни игроков. OpenDota
    же тратит запрос на игрока и делит свои ~2000 в сутки с коллекторами,
    из-за чего первый живой прогон дал 76 ответов из 200 спрошенных.
    """
    name = name or os.getenv("RANKS_RESOLVER") or (
        "stratz" if os.getenv("STRATZ_API_TOKEN") else "opendota")
    if name == "stratz":
        return StratzRankResolver(
            os.getenv("STRATZ_API_TOKEN", ""),
            batch_size=int(os.getenv("RANKS_BATCH_SIZE",
                                     str(StratzRankResolver.DEFAULT_BATCH))))
    return OpenDotaRankResolver(
        api_key=os.getenv("OPENDOTA_API_KEY") or None,
        api_delay_s=float(os.getenv("OPENDOTA_DELAY_S", "1.1")))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="collector.ranks",
                                description="кэш рангов по account_id")
    p.add_argument("command",
                   choices=("seed", "fill", "report", "probe", "harvest",
                            "scan", "queue"))
    p.add_argument("--matches", type=int,
                   default=int(os.getenv("RANKS_SEED_MATCHES", "1000")),
                   help="seed: сколько матчей потока просмотреть")
    p.add_argument("--budget", type=int,
                   default=int(os.getenv("RANKS_FILL_BUDGET", "200")),
                   help="fill: сколько аккаунтов опросить")
    p.add_argument("--resolver", default=None, choices=("opendota", "stratz"),
                   help="по умолчанию stratz при наличии токена")
    p.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    p.add_argument("--limit", type=int, default=None,
                   help="harvest: сколько матчей архива прочитать")
    p.add_argument("--interval", type=int, default=0,
                   help="повторять команду каждые N секунд (0 — один раз)")
    args = p.parse_args(argv)

    if args.command == "probe":
        print(probe_stratz(os.getenv("STRATZ_API_TOKEN", "")))
        return 0

    dsn = os.getenv("POSTGRES_DSN",
                    "postgresql://dota:dota_dev_password@localhost:5432/manta")
    cache = RankCache(dsn)
    try:
        if args.interval:
            return _loop(cache, dsn, args)
        return _run_once(cache, dsn, args)
    finally:
        cache.close()


def _run_once(cache: RankCache, dsn: str, args) -> int:
    """Один прогон команды. Вынесено ради --interval: цикл не должен
    знать, что именно он повторяет."""
    if args.command == "seed":
        stream = SteamMatchStream(
            os.getenv("STEAM_API_KEY", ""),
            api_delay_s=float(os.getenv("STEAM_DELAY_S", "1.0")))
        stats = seed(cache, stream, args.matches)
        print("seed:", ", ".join(f"{k} {v}" for k, v in stats.items()))
        if stats["слотов"]:
            print(f"      видимых игроков на матч: "
                  f"{visible_per_match(stats):.1f} из 10 "
                  f"(скрытых профилей "
                  f"{100.0 * stats['скрытых'] / stats['слотов']:.0f}%)")
    elif args.command == "scan":
        stream = SteamMatchStream(
            os.getenv("STEAM_API_KEY", ""),
            api_delay_s=float(os.getenv("STEAM_DELAY_S", "1.0")))
        queue = CandidateQueue(dsn)
        try:
            funnel, sweep = scan(cache, stream, args.matches, queue=queue)
            queued = queue.stats()
        finally:
            queue.close()
        total = funnel.pop("всего матчей", 0)
        print(f"=== воронка отбора ({total} матчей потока) ===")
        for why, n in sorted(funnel.items(), key=lambda kv: -kv[1]):
            print(f"{why:>22}: {n:>6}  {100.0 * n / max(total, 1):5.2f}%")
        print()
        print("=== доля потока при разных порогах ===")
        print(sweep_table(sweep, total))
        print()
        print("=== очередь кандидатов ===")
        for state, n in queued.items():
            print(f"{state:>22}: {n}")
        print()
    elif args.command == "queue":
        q = CandidateQueue(dsn)
        try:
            print(queue_report(q))
        finally:
            q.close()
        return 0
    elif args.command == "harvest":
        from .rawstore import RawMatchStore
        store = RawMatchStore.from_env()
        if store is None:
            print("хранилище сырого JSON недоступно (RAW_MATCH_STORE=0"
                  " или S3 не поднят)")
            return 1
        stats = harvest_rawstore(cache, store, limit=args.limit)
        print("harvest:", ", ".join(f"{k} {v}" for k, v in stats.items()))
    elif args.command == "fill":
        resolver = build_resolver(args.resolver)
        logger.info("резолвер: %s", resolver.name)
        try:
            stats = fill(cache, resolver, args.budget, ttl_days=args.ttl_days)
        except StratzAuthRefused as exc:
            # Внятный отказ вместо сорока одинаковых предупреждений.
            # Автоматически переключиться на OpenDota было бы соблазнительно
            # и неверно: у него квота делится с коллекторами, и молча
            # потратить её за владельца — не наше решение.
            print(f"fill: {exc}.\n"
                  "  Токен STRATZ недействителен или отозван. Проверить:\n"
                  "    python -m collector.ranks probe\n"
                  "  Взять ранги у OpenDota (тратит его суточную квоту):\n"
                  "    python -m collector.ranks fill --resolver opendota")
            return 1
        print("fill:", ", ".join(f"{k} {v}" for k, v in stats.items()))
    print(report(cache))
    return 0


def _loop(cache: RankCache, dsn: str, args) -> int:
    """Повторять команду вечно. Единственная задача — НЕ УМИРАТЬ.

    Сбой одного прогона (сеть отвалилась, Valve отдал 429, Postgres
    перезапустился) не должен останавливать конвейер: следующий заход
    через интервал почти всегда проходит. Молча ронять фоновый процесс мы
    уже проходили — реплейный путь простоял так 82 часа при зелёном
    pgrep, и стоило это недели данных.
    """
    logger.info("цикл %s каждые %d с", args.command, args.interval)
    while True:
        started = time.monotonic()
        try:
            _run_once(cache, dsn, args)
        except KeyboardInterrupt:
            return 0
        except Exception:  # noqa: BLE001 — цикл обязан пережить всё
            logger.exception("прогон %s упал, продолжаем", args.command)
            cache = _reconnect(cache, dsn)
        sleep_s = max(args.interval - (time.monotonic() - started), 1.0)
        time.sleep(sleep_s)


def _reconnect(cache: RankCache, dsn: str) -> RankCache:
    """Пересоздать соединение с Postgres после его перезапуска.

    Инцидент 2026-07-20: коллекторы держали мёртвое соединение и падали
    на первом же запросе каждого цикла до ручного перезапуска процесса.
    """
    try:
        cache.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        return RankCache(dsn)
    except Exception:  # noqa: BLE001 — попробуем в следующем цикле
        logger.warning("Postgres недоступен, повтор в следующем цикле")
        return cache


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
