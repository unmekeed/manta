"""Очередь кандидатов на скачивание реплея (спринт 126).

Разделение труда, ради которого очередь и существует. Найти подходящий
матч дёшево: один вызов Valve отдаёт сто матчей, и отбор по кэшу рангов
не стоит вообще ничего. Скачать его дорого: 58 МиБ и, по замеру,
24-50 Мбит/с — несколько тысяч матчей в сутки физического потолка.

Раз находим быстрее, чем качаем, между этими шагами обязана стоять
очередь, и обязана она стоять в БД: между «нашли» и «скачали» лежат
часы, перезапуски процессов и суточные лимиты OpenDota.

Второе назначение таблицы — проверка ТОЧНОСТИ правила отбора. Правило
берёт матч по двум известным рангам из десяти игроков, и это допущение,
а не факт. Сохраняя, почему именно матч прошёл, мы получаем право
спросить потом: а действительно ли собранное оказалось Immortal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

logger = logging.getLogger("collector.candidates")

# Сколько дней Valve держит реплей. Официально «около двух недель»;
# берём с запасом вниз, потому что цена ошибки несимметрична: слишком
# ранний отказ теряет один матч, слишком поздний — жжёт квоту OpenDota на
# матче, которого уже нет.
REPLAY_TTL_DAYS = 13

# Сколько раз спрашивать соль, прежде чем признать матч безнадёжным.
MAX_SALT_ATTEMPTS = 8

# На сколько откладывать повтор, если соли ещё нет. У свежего матча она
# появляется с задержкой, и долбиться каждый цикл значит тратить квоту
# впустую.
RETRY_MINUTES = 30

# Через сколько кандидат, выданный коллектору, считается потерянным.
# Скачивание одного реплея — секунды-десятки секунд; получас с запасом
# перекрывает самый медленный случай и при этом быстро возвращает в
# очередь то, что зависло из-за смерти процесса.
STALE_TAKEN_MINUTES = 30


@dataclass(frozen=True)
class Candidate:
    match_id: int
    match_seq_num: int
    started_at: object | None
    known_ranks: int
    immortal_ranks: int
    avg_known_rank: int


class CandidateQueue:
    """Таблица ReplayCandidates: кого нашли, кого отдали, кого скачали."""

    def __init__(self, dsn: str) -> None:
        self._db = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self._db.close()

    # -- запись ---------------------------------------------------------------

    def add(self, rows: list[Candidate]) -> int:
        """Добавить найденных. Повторная находка НЕ трогает состояние.

        Сканирование может пройти по одному участку потока дважды
        (перезапуск, откат курсора), и уже скачанный матч не должен
        вернуться в очередь — иначе мы качали бы его снова и снова.
        """
        if not rows:
            return 0
        with self._db.cursor() as cur:
            cur.executemany(
                "INSERT INTO ReplayCandidates (match_id, match_seq_num,"
                "  started_at, known_ranks, immortal_ranks, avg_known_rank) "
                "VALUES (%s, %s, to_timestamp(%s), %s, %s, %s) "
                "ON CONFLICT (match_id) DO NOTHING",
                [(c.match_id, c.match_seq_num, c.started_at, c.known_ranks,
                  c.immortal_ranks, c.avg_known_rank) for c in rows])
        return len(rows)

    # -- выдача ---------------------------------------------------------------

    def expire(self, ttl_days: int = REPLAY_TTL_DAYS) -> int:
        """Пометить кандидатов, чьи реплеи Valve уже удалил."""
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE ReplayCandidates SET state = 'expired',"
                "       updated_at = NOW() "
                " WHERE state = 'new' AND started_at IS NOT NULL "
                "   AND started_at < NOW() - make_interval(days => %s)",
                (ttl_days,))
            return cur.rowcount

    def requeue_stale_taken(self, minutes: int = STALE_TAKEN_MINUTES) -> int:
        """Вернуть в очередь кандидатов, застрявших в `taken`.

        Visibility timeout. Кандидат помечается `taken` при выдаче, а
        `done` — после скачивания; если процесс между этими моментами
        умер (рестарт WSL, kill, падение), строка осталась бы в `taken`
        навсегда, потому что очередь выбирает только `new`.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE ReplayCandidates "
                "   SET state = 'new', next_try_at = NULL, updated_at = NOW(),"
                "       last_error = 'зависло в taken' "
                " WHERE state = 'taken' "
                "   AND updated_at < NOW() - make_interval(mins => %s)",
                (minutes,))
            return cur.rowcount

    def take(self, limit: int) -> list[Candidate]:
        """Взять из очереди. Самые СВЕЖИЕ вперёд.

        Порядок изменён по замеру (спринт 128.1). Изначально брали самых
        старых — «они ближе к истечению». Это верно, только пока приток
        МЕНЬШЕ пропускной способности. Замер показал обратное: сканер
        находит ~3500 кандидатов в сутки, а скачать канал позволяет ~1900.
        При таком избытке «старые вперёд» означает, что мы всегда качаем
        подтухший хвост очереди, приближаясь к границе в 13 дней, а
        свежие ждут своей очереди вечно.
        
        Кандидат не ценность — их больше, чем мы способны взять. Ценность
        — пропускная способность канала, и тратить её надо на реплей с
        максимальным запасом жизни. Излишек пусть истекает.
        """
        if limit <= 0:
            return []
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT match_id, match_seq_num, started_at, known_ranks,"
                "       immortal_ranks, avg_known_rank "
                "  FROM ReplayCandidates "
                " WHERE state = 'new' "
                "   AND (next_try_at IS NULL OR next_try_at <= NOW()) "
                " ORDER BY found_at DESC LIMIT %s", (limit,))
            return [Candidate(int(r[0]), int(r[1]), r[2], int(r[3]),
                              int(r[4]), int(r[5])) for r in cur.fetchall()]

    def record_truth(self, match_id: int, known: int, immortal: int,
                     avg_rank: int) -> None:
        """Записать ФАКТИЧЕСКИЙ состав матча по данным OpenDota.

        Приезжает вместе с солью (см. миграцию 009) и не стоит ни одного
        дополнительного вызова. Единственный способ узнать, не набирает
        ли правило отбора мусор.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE ReplayCandidates "
                "   SET true_known_ranks = %s, true_immortal_ranks = %s,"
                "       true_avg_rank = %s "
                " WHERE match_id = %s", (known, immortal, avg_rank, match_id))

    def precision(self, min_rank: int = 80) -> dict[str, int]:
        """Сколько отобранного оказалось иммортальным на самом деле.

        Считается ТОЛЬКО по матчам, где факт известен: NULL означает «не
        смотрели», и включать такие в знаменатель значило бы разбавлять
        точность собственным незнанием.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT count(*),"
                "       count(*) FILTER (WHERE true_known_ranks > 0),"
                "       count(*) FILTER (WHERE true_known_ranks > 0"
                "                          AND true_avg_rank >= %s),"
                "       coalesce(avg(true_avg_rank) FILTER"
                "                (WHERE true_known_ranks > 0), 0),"
                "       coalesce(avg(true_known_ranks) FILTER"
                "                (WHERE true_known_ranks > 0), 0)"
                "  FROM ReplayCandidates WHERE state = 'done'",
                (min_rank,))
            row = cur.fetchone()
        return {"скачано": int(row[0]), "факт известен": int(row[1]),
                "из них immortal": int(row[2]),
                "средний ранг": int(row[3]), "рангов на матч": int(row[4])}

    def mark(self, match_id: int, state: str, error: str | None = None) -> None:
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE ReplayCandidates "
                "   SET state = %s, last_error = %s, updated_at = NOW() "
                " WHERE match_id = %s", (state, error, match_id))

    def defer(self, match_id: int, minutes: int = RETRY_MINUTES,
              error: str | None = None,
              max_attempts: int = MAX_SALT_ATTEMPTS) -> str:
        """Отложить повтор; исчерпав попытки — признать безнадёжным.

        Возвращает новое состояние, чтобы вызывающий мог сосчитать
        причины в статистике цикла, а не гадать по логу.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE ReplayCandidates "
                "   SET attempts = attempts + 1, "
                "       state = CASE WHEN attempts + 1 >= %s "
                "                    THEN 'no_salt' ELSE 'new' END, "
                "       next_try_at = NOW() + make_interval(mins => %s), "
                "       last_error = %s, updated_at = NOW() "
                " WHERE match_id = %s RETURNING state",
                (max_attempts, minutes, error, match_id))
            row = cur.fetchone()
        return row[0] if row else "new"

    # -- отчёт ----------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._db.cursor() as cur:
            cur.execute("SELECT state, count(*) FROM ReplayCandidates "
                        " GROUP BY state ORDER BY count(*) DESC")
            return {str(s): int(n) for s, n in cur.fetchall()}

    def precision_sample(self, limit: int = 20) -> list[tuple[int, int, int]]:
        """(match_id, известных рангов, средний ранг) по скачанным.

        Материал для проверки правила: правда ли отобранное оказалось
        иммортальным. Сверять надо с внешним источником, поэтому здесь
        только выборка, а не вердикт.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT match_id, known_ranks, avg_known_rank "
                "  FROM ReplayCandidates WHERE state = 'done' "
                " ORDER BY updated_at DESC LIMIT %s", (limit,))
            return [(int(a), int(b), int(c)) for a, b, c in cur.fetchall()]
