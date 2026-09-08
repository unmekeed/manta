"""Общая память источников об отказах (спринт 204).

ЗАЧЕМ. Кандидаты берутся из листинга OpenDota и делятся пополам между
двумя источниками деталей. Но источники НЕ РАВНОСИЛЬНЫ на этих
кандидатах: OpenDota отдаёт детали любого матча из своего же листинга,
STRATZ — только тех, что успел разобрать у себя, то есть примерно трети.

Замер 08.09.2026 по трём циклам: из доли STRATZ он не смог 70%, что
составляет 36% ВСЕГО публичного потока. Эти матчи не доходят до OpenDota
никогда — фильтр доли отсекает их ещё до запроса деталей.

Отказавшийся публикует match_id здесь; напарник видит и берёт сверх своей
доли. Та же мысль, что в `PartnerSplit`: доля не держится за тем, кто не
может собирать. Только поштучно, а не оптом.

ПОЧЕМУ В БАЗЕ, А НЕ В ПАМЯТИ. Во-первых, читать должен ДРУГОЙ процесс.
Во-вторых, память процесса умирает с процессом: `_pending` и `_rejected`
в stratz.py стирались при каждом перезапуске контейнера, и все отвергнутые
матчи спрашивались заново, по три попытки каждый. 8 сентября контейнеры
пересоздавались шесть раз за день.

ОТКАЗ ЗАПИСИ НЕ ОСТАНАВЛИВАЕТ СБОР. Общая память — ускоритель, а не
условие работы: недоступная база означает, что напарник не узнает про
отвергнутый матч, то есть ровно то поведение, что было до этого спринта.
Ронять из-за этого цикл значило бы менять «собираем медленнее» на «не
собираем вовсе».
"""
from __future__ import annotations

import logging

logger = logging.getLogger("collector.declines")

UPSERT_SQL = """
INSERT INTO SourceDeclines (match_id, source_name, reason, attempts)
VALUES (%(match_id)s, %(source_name)s, %(reason)s, %(attempts)s)
ON CONFLICT (match_id, source_name) DO UPDATE
   SET reason      = EXCLUDED.reason,
       attempts    = SourceDeclines.attempts + EXCLUDED.attempts,
       declined_at = NOW()
"""

# Что отвергнуто напарником за окно. Окно нужно, потому что таблица
# растёт вечно, а брать сверх доли имеет смысл только свежее: матч
# недельной давности уже уехал из листинга, и спрашивать про него
# некого.
RECENT_SQL = """
SELECT match_id FROM SourceDeclines
 WHERE source_name = %(source_name)s
   AND declined_at > NOW() - (%(hours)s * INTERVAL '1 hour')
"""

# Чем старше отказ, тем он бесполезнее: матч уехал из окна листинга, и
# напарнику он уже не встретится. Держать такие строки — растить таблицу
# ради данных, на которые никто не смотрит.
PRUNE_SQL = """
DELETE FROM SourceDeclines
 WHERE declined_at < NOW() - (%(hours)s * INTERVAL '1 hour')
"""


def record(db, source_name: str, match_id: int, reason: str,
           attempts: int = 1) -> bool:
    """Отметить, что источник забрал матч и не смог. True — записано.

    Возвращаемое значение существует ради теста: в бою с неудачей нечего
    делать, кроме как сказать о ней в лог.
    """
    try:
        with db.cursor() as cur:
            cur.execute(UPSERT_SQL, {"match_id": int(match_id),
                                     "source_name": source_name,
                                     "reason": reason,
                                     "attempts": int(attempts)})
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — общая память не условие работы
        logger.warning("отказ по матчу %s не записан: %s", match_id, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def declined_by(db, source_name: str, hours: float = 6.0) -> set[int]:
    """Матчи, которые напарник забрал и не смог за последние `hours`.

    Пустое множество при недоступной базе — НЕ ошибка и не повод
    остановиться: это возврат к прежнему поведению, когда общей памяти не
    было вовсе.
    """
    try:
        with db.cursor() as cur:
            cur.execute(RECENT_SQL, {"source_name": source_name,
                                     "hours": hours})
            return {int(r[0]) for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("отказы напарника %s не прочитаны: %s",
                       source_name, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return set()


def prune(db, hours: float = 72.0) -> int:
    """Убрать протухшие отказы; вернуть, сколько удалено."""
    try:
        with db.cursor() as cur:
            cur.execute(PRUNE_SQL, {"hours": hours})
            n = cur.rowcount or 0
        db.commit()
        return n
    except Exception as exc:  # noqa: BLE001
        logger.warning("протухшие отказы не убраны: %s", exc)
        return 0
