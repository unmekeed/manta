"""Журнал решений промоушен-гейта (спринт 202).

ЗАЧЕМ. Решения гейта жили ТОЛЬКО в Telegram. Храповик (ROADMAP, G1) стал
виден лишь потому, что владелец прислал в чат четыре сообщения подряд и
их можно было сравнить глазами: про-эталон prod прошёл 0.1553 → 0.1565 →
0.1565 → 0.1578, каждый шаг «в пределах шума», сумма — заметное
ухудшение. Гейт сравнивает кандидата только с текущим prod, и планка
пересчитывается от неё же, поэтому серия незначимых ухудшений проходит
беспрепятственно.

Чат — не база: в нём нельзя сделать запрос, а сообщения тонут в потоке.
С таблицей вопрос «насколько съехал prod за десять промоушенов»
становится запросом, а не археологией.

ЗАПИСЬ НЕ ИМЕЕТ ПРАВА МЕШАТЬ ПРОМОУШЕНУ. Модель — продукт, журнал — её
дневник. Недоступная база, отсутствующая миграция, опечатка в DSN не
должны отменять продвижение хорошей версии: `record()` ловит всё и
ограничивается предупреждением. Обратный порядок приоритетов означал бы,
что мелкая беда со вспомогательной таблицей стоит пользователю модели, —
ровно то соображение, по которому карточка матча пишется после отчёта, а
не вместо него.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("training.history")

UPSERT_SQL = """
INSERT INTO ModelVersions (model_name, version, promoted, reason,
                           dataset_matches, metrics, holdouts)
VALUES (%(model_name)s, %(version)s, %(promoted)s, %(reason)s,
        %(dataset_matches)s, %(metrics)s, %(holdouts)s)
ON CONFLICT (model_name, version) DO UPDATE
   SET promoted        = EXCLUDED.promoted,
       reason          = EXCLUDED.reason,
       dataset_matches = EXCLUDED.dataset_matches,
       metrics         = EXCLUDED.metrics,
       holdouts        = EXCLUDED.holdouts,
       decided_at      = NOW()
"""


def row(model_name: str, version: str, promoted: bool, reason: str,
        artifact: dict, holdouts: list) -> dict:
    """Параметры UPSERT из того, что уже посчитано ради решения.

    Ничего не пересчитывает: журнал обязан записывать РЕШЕНИЕ, а не своё
    мнение о нём. Посчитай он метрику сам — она разошлась бы с той, по
    которой на самом деле продвигали, и разбор храповика опирался бы на
    числа, которых гейт не видел.
    """
    dataset = artifact.get("dataset") or {}
    return {
        "model_name": model_name,
        "version": version,
        "promoted": bool(promoted),
        "reason": reason or "",
        "dataset_matches": int(dataset.get("matches") or 0),
        "metrics": json.dumps(artifact.get("metrics") or {},
                              ensure_ascii=False),
        "holdouts": json.dumps(holdouts or [], ensure_ascii=False),
    }


def record(model_name: str, version: str, promoted: bool, reason: str,
           artifact: dict, holdouts: list, dsn: str | None = None) -> bool:
    """Записать решение. True — записано; False — нет, и это не беда.

    Возвращаемое значение существует ради теста: в бою его никто не
    смотрит, потому что смотреть не на что — сделать с неудачей нечего,
    кроме как сказать о ней в лог.
    """
    dsn = dsn or os.getenv(
        "POSTGRES_DSN",
        "postgresql://dota:dota_dev_password@localhost:5432/manta")
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as db:
            db.execute(UPSERT_SQL, row(model_name, version, promoted, reason,
                                       artifact, holdouts))
        return True
    except Exception as exc:  # noqa: BLE001 — журнал не мешает продукту
        logger.warning("решение гейта не записано в историю: %s", exc)
        return False
