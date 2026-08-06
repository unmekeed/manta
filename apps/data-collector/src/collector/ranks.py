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
import time
from collections import Counter
from typing import Iterable, Protocol

import psycopg
import requests

from .sources.steam import (ANONYMOUS_ACCOUNT_ID, SteamAPIError,
                            SteamMatchStream, match_accounts)

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

    def save(self, resolved: dict[int, int], source: str) -> int:
        """Записать ответы резолвера. Ключ есть — значит ответ ПОЛУЧЕН.

        Отсутствие аккаунта в resolved и rank_tier=0 — разные вещи: первое
        означает «спросить не удалось, вернёмся позже», второе — «спросили,
        ранга нет». Резолвер обязан соблюдать это различие, иначе сетевой
        сбой навсегда пометит живого Immortal как безрангового.
        """
        if not resolved:
            return 0
        ids = list(resolved)
        ranks = [int(resolved[a]) for a in ids]
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO PlayerRanks (account_id, rank_tier, source,"
                "                         checked_at) "
                "SELECT u.a, u.r, %s, NOW() "
                "  FROM unnest(%s::bigint[], %s::smallint[]) AS u(a, r) "
                "ON CONFLICT (account_id) DO UPDATE "
                "  SET rank_tier = EXCLUDED.rank_tier, "
                "      source = EXCLUDED.source, checked_at = NOW()",
                (source, ids, ranks))
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
                "                (WHERE rank_tier >= %s), 0)"
                "  FROM PlayerRanks", (min_rank, min_rank))
            row = cur.fetchone()
        keys = ("всего", "не опрошено", "без ранга", "immortal",
                "встреч всего", "встреч immortal")
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
                    logger.debug("opendota %s: HTTP %s", aid, resp.status_code)
                    continue
                body = resp.json() or {}
            except (requests.RequestException, ValueError) as exc:
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


def is_admin_only(message: str) -> bool:
    """Отказ «эндпоинт только для админов» — не сетевой сбой, а приговор."""
    return "not an admin" in message.lower()


class StratzRankResolver:
    """Ранг из player/players STRATZ. Быстрый путь — если пустят.

    Пакетный `players(steamAccountIds:)` был бы прямым множителем: квота
    считается по HTTP-запросам, а не по игрокам. Но ровно такой же
    пакетный `matches(ids:)` оказался админским (спринт 121/122), поэтому
    пакет здесь НЕ предполагается, а ПРОВЕРЯЕТСЯ: первый отказ вида «User
    is not an admin» навсегда переводит резолвер на одиночные запросы и
    пишет об этом в лог. Гадать мы уже пробовали, вышло дорого.
    """

    name = "stratz"

    def __init__(self, token: str, api_url: str = STRATZ_API_URL,
                 timeout: float = 30.0, batch_size: int = 50,
                 rank_field: str | None = None) -> None:
        if not token:
            raise ValueError("stratz: пустой токен")
        self._token = token
        self._url = api_url
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._field = rank_field or STRATZ_RANK_FIELD
        # None — не проверяли, True — пакет работает, False — админский.
        self.batch_allowed: bool | None = None
        self._session = requests.Session()

    # -- транспорт -------------------------------------------------------------

    def _gql(self, query: str, variables: dict) -> dict:
        resp = self._session.post(
            self._url, json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {self._token}", **STRATZ_UA},
            timeout=self._timeout)
        body = resp.json() if resp.content else {}
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            raise StratzRankError(str(errors[0].get("message", errors)))
        if resp.status_code != 200:
            raise StratzRankError(f"HTTP {resp.status_code}")
        return (body or {}).get("data") or {}

    # -- запросы ---------------------------------------------------------------

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
        for start in range(0, len(account_ids), self._batch_size):
            chunk = account_ids[start:start + self._batch_size]
            try:
                batched = self._try_batch(chunk)
            except StratzRankError as exc:
                logger.warning("STRATZ пакет %d id: %s", len(chunk), exc)
                batched = None
            if batched is not None:
                out.update(batched)
                continue
            for aid in chunk:
                try:
                    data = self._gql(self._single_query(), {"id": aid})
                except StratzRankError as exc:
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

def classify_match(ranks: dict[int, int | None], min_rank: int = IMMORTAL_MIN_RANK,
                   min_known: int = 4, min_share: float = 0.6
                   ) -> tuple[bool, str]:
    """Брать ли матч, зная ранги его игроков.

    Требовать все десять известных рангов нельзя: часть профилей закрыта
    навсегда, часть аккаунтов мы ещё не опрашивали, и такой матч не
    станет пригодным никогда. Поэтому решение принимается по ДОЛЕ среди
    известных, при минимуме известных — иначе один случайный Immortal из
    одного известного игрока протащил бы любой матч.

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
    stats = {"матчей": 0, "аккаунтов": 0, "вызовов": 0}
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
            seen_batch.extend(match_accounts(m))
            seq = max(seq, int(m.get("match_seq_num") or seq) + 1)
        stats["матчей"] += len(got)
    stats["аккаунтов"] = cache.see(seen_batch)
    cache.set_cursor(seq)
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
    stats = {"спрошено": len(todo), "получено": 0, "immortal": 0}
    for start in range(0, len(todo), chunk):
        part = todo[start:start + chunk]
        resolved = resolver.resolve(part)
        if not resolved:
            continue
        cache.save(resolved, resolver.name)
        stats["получено"] += len(resolved)
        stats["immortal"] += sum(1 for r in resolved.values()
                                 if r >= IMMORTAL_MIN_RANK)
    return stats


def report(cache: RankCache, min_rank: int = IMMORTAL_MIN_RANK) -> str:
    c = cache.counts(min_rank)
    lines = ["=== кэш рангов ==="]
    for key in ("всего", "не опрошено", "без ранга", "immortal"):
        lines.append(f"{key:>14}: {c[key]}")
    asked = c["всего"] - c["не опрошено"]
    if asked:
        lines.append(f"{'доля immortal':>14}: "
                     f"{100.0 * c['immortal'] / asked:.1f}% от опрошенных")
    # Ключевое число всего спринта: какую долю ПОТОКА (а не словаря)
    # закрывают известные Immortal. Оно и решает, сколько матчей в сутки
    # своя разбивка сможет отобрать.
    if c["встреч всего"]:
        lines.append(f"{'доля потока':>14}: "
                     f"{100.0 * c['встреч immortal'] / c['встреч всего']:.2f}% "
                     f"встреч приходится на известных immortal")
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


def build_resolver(name: str) -> RankResolver:
    if name == "stratz":
        token = os.getenv("STRATZ_API_TOKEN", "")
        return StratzRankResolver(
            token, batch_size=int(os.getenv("RANKS_BATCH_SIZE", "50")))
    return OpenDotaRankResolver(
        api_key=os.getenv("OPENDOTA_API_KEY") or None,
        api_delay_s=float(os.getenv("OPENDOTA_DELAY_S", "1.1")))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="collector.ranks",
                                description="кэш рангов по account_id")
    p.add_argument("command", choices=("seed", "fill", "report", "probe"))
    p.add_argument("--matches", type=int,
                   default=int(os.getenv("RANKS_SEED_MATCHES", "1000")),
                   help="seed: сколько матчей потока просмотреть")
    p.add_argument("--budget", type=int,
                   default=int(os.getenv("RANKS_FILL_BUDGET", "200")),
                   help="fill: сколько аккаунтов опросить")
    p.add_argument("--resolver", default=os.getenv("RANKS_RESOLVER", "opendota"),
                   choices=("opendota", "stratz"))
    p.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    args = p.parse_args(argv)

    if args.command == "probe":
        print(probe_stratz(os.getenv("STRATZ_API_TOKEN", "")))
        return 0

    dsn = os.getenv("POSTGRES_DSN",
                    "postgresql://dota:dota_dev_password@localhost:5432/manta")
    cache = RankCache(dsn)
    try:
        if args.command == "seed":
            stream = SteamMatchStream(
                os.getenv("STEAM_API_KEY", ""),
                api_delay_s=float(os.getenv("STEAM_DELAY_S", "1.0")))
            stats = seed(cache, stream, args.matches)
            print("seed:", ", ".join(f"{k} {v}" for k, v in stats.items()))
        elif args.command == "fill":
            stats = fill(cache, build_resolver(args.resolver), args.budget,
                         ttl_days=args.ttl_days)
            print("fill:", ", ".join(f"{k} {v}" for k, v in stats.items()))
        print(report(cache))
    finally:
        cache.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
