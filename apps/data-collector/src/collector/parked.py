"""Парковка недостижимых реплеев (спринт 153).

ЗАЧЕМ. The International 2026 играется на китайском кластере Valve, и
одиннадцать из двенадцати последних про-матчей раздаются с
replay413.dota2.com.cn. Ни VPS в Ирландии, ни домашняя машина до него не
доходят — проверено с обеих.

Коллектор вёл себя ровно как велено в спринте 118: три попытки, потом
«сдвигаю курсор, чтобы не блокировать очередь». Правило верное — вставший
курсор в 2026-07-31 стоил 82 часов простоя. Но у него есть цена, которую
до сих пор не называли: курсор монотонный, и сдвинутый матч не вернётся
НИКОГДА. Появись маршрут завтра — реплеи TI всё равно потеряны.

Парковка разводит эти два решения. Очередь по-прежнему не блокируется, а
запись остаётся: `--source parked` вернётся к ней, когда маршрут появится.

ПРО ОБРЫВ СОЕДИНЕНИЯ КАК ПРИЗНАК. Паркуем только ConnectTimeout — это
однозначное «до хоста не достучались». Обрыв в середине передачи
приходит другими исключениями и обрабатывается выше как временный сбой:
файл там цел, повторить его стоит того же, что и в первый раз.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger("collector.parked")


def replay_host(url: str) -> str:
    """Хост из адреса реплея; пустая строка, если разобрать нечем."""
    try:
        return urlparse(str(url or "")).hostname or ""
    except ValueError:
        return ""


def is_unreachable(exc: BaseException) -> bool:
    """Соединение не установилось — значит хост недоступен отсюда.

    Только ConnectTimeout, и это осознанно узко. Прочие сетевые ошибки
    (обрыв в середине передачи, сброс, DNS) означают разное, и часть из
    них временная; записать их в недостижимость значило бы парковать
    матчи, которые скачались бы со второй попытки.
    """
    return isinstance(exc, requests.exceptions.ConnectTimeout)


class UnreachableHosts:
    """Хосты, к которым сейчас бессмысленно обращаться.

    ЗАЧЕМ. Один недостижимый матч стоит около 80 секунд: DNS отдаёт
    несколько адресов, и каждый пробуется по таймауту соединения. При
    двух матчах за цикл и трёх попытках на матч очередь двигалась на два
    матча за три часа — а перед ней сотни матчей одного турнира, все на
    одном хосте.

    Узнав, что хост не отвечает, мы перестаём набирать его номер: матчи
    с этого хоста паркуются сразу, без сети. Очередь идёт втрое быстрее,
    и ни один матч при этом не теряется — в том и смысл парковки.

    ПОЧЕМУ СО СРОКОМ ГОДНОСТИ. Маршруты меняются: сегодня хост
    недостижим, завтра поднимут прокси. Вечная отметка превратила бы
    временную беду в постоянную, и заметить это было бы нечем — в логе
    ровным счётом ничего не происходит.
    """

    def __init__(self, *, threshold: int = 2, ttl_s: float = 6 * 3600,
                 clock=None) -> None:
        self._threshold = threshold
        self._ttl = ttl_s
        self._clock = clock or time.monotonic
        self._fails: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def record_failure(self, host: str) -> bool:
        """Отметить неудачу; True — хост только что признан недостижимым."""
        if not host:
            return False
        self._fails[host] = self._fails.get(host, 0) + 1
        if self._fails[host] < self._threshold or self.is_unreachable(host):
            return False
        self._until[host] = self._clock() + self._ttl
        logger.warning(
            "хост %s не отвечает (%d раза подряд) — матчи с него паркую без "
            "попыток на %.0f ч", host, self._fails[host], self._ttl / 3600)
        return True

    def record_success(self, host: str) -> None:
        """Хост ответил — забываем всё, что о нём знали."""
        self._fails.pop(host, None)
        self._until.pop(host, None)

    def is_unreachable(self, host: str) -> bool:
        until = self._until.get(host)
        if until is None:
            return False
        if self._clock() >= until:
            # Срок вышел: даём хосту второй шанс с чистого листа, иначе
            # одна старая неудача навсегда держала бы его на пороге.
            self._until.pop(host, None)
            self._fails.pop(host, None)
            return False
        return True


class ParkedStore:
    """Список желаний: матчи, чей реплей хотели и не смогли взять.

    Соединение берётся ЧЕРЕЗ ВЫЗОВ, а не сохраняется. Коллектор
    пересоздаёт его сам, когда контейнер Postgres перезапустили
    (инцидент 2026-07-20), и сохранённая ссылка указывала бы на закрытое
    соединение — то есть парковка ломалась бы ровно в тот момент, когда
    сбои и случаются.
    """

    def __init__(self, db) -> None:
        self._get_db = db if callable(db) else (lambda: db)

    @property
    def _db(self):
        return self._get_db()

    def park(self, match_id: int, replay_url: str, reason: str) -> None:
        """Записать матч в парковку; повторная парковка копит счётчик."""
        with self._db.cursor() as cur:
            cur.execute(
                """INSERT INTO ParkedReplays
                       (match_id, replay_url, host, reason)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (match_id) DO UPDATE
                       SET attempts    = ParkedReplays.attempts + 1,
                           reason      = EXCLUDED.reason,
                           replay_url  = EXCLUDED.replay_url,
                           last_parked = NOW()""",
                (int(match_id), str(replay_url), replay_host(replay_url),
                 str(reason)[:200]))

    def wanted(self, limit: int) -> list[tuple[int, str]]:
        """[(match_id, replay_url)] — припаркованные и ВСЁ ЕЩЁ нужные.

        Матч, у которого реплей уже появился, из выдачи исчезает сам:
        желание исполнено. Удалять строку в обработчике успеха значило бы
        завести вторую точку правды о том, что матч собран, — а первая
        уже есть, и это CollectedMatches.has_replay.

        Старые вперёд: реплеи Valve снимает с раздачи со временем, и у
        давно припаркованных шанс тает быстрее.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """SELECT p.match_id, p.replay_url
                     FROM ParkedReplays p
                     LEFT JOIN CollectedMatches c
                            ON c.match_id = p.match_id AND c.has_replay
                    WHERE c.match_id IS NULL
                    ORDER BY p.match_id
                    LIMIT %s""", (int(limit),))
            return [(int(r[0]), str(r[1])) for r in cur.fetchall()]

    def prune(self) -> int:
        """Убрать исполненные желания; возвращает число удалённых строк.

        Необязательная уборка: невыполненный prune не портит ничего,
        потому что wanted() и так их не отдаёт.
        """
        with self._db.cursor() as cur:
            cur.execute(
                """DELETE FROM ParkedReplays p
                    USING CollectedMatches c
                    WHERE c.match_id = p.match_id AND c.has_replay""")
            return cur.rowcount or 0

    def stats(self) -> list[tuple[str, int]]:
        """[(хост, сколько ждёт)] — по чему именно стоит очередь."""
        with self._db.cursor() as cur:
            cur.execute(
                """SELECT p.host, count(*)
                     FROM ParkedReplays p
                     LEFT JOIN CollectedMatches c
                            ON c.match_id = p.match_id AND c.has_replay
                    WHERE c.match_id IS NULL
                    GROUP BY p.host ORDER BY count(*) DESC""")
            return [(str(r[0]), int(r[1])) for r in cur.fetchall()]
