"""Retention-политика отчётов (Гл. 9.7, остаток находки S6; спринт 72).

Зачем. MatchReports растут без ограничения, а каждый отчёт содержит
персональные данные (ник или его псевдоним) вместе с разбором игры.
GDPR требует не хранить их дольше, чем нужно для цели обработки:
«бессрочно, потому что место есть» — не цель.

Что важно понимать перед включением: отчёт — ПРОИЗВОДНЫЙ артефакт.
Исходные данные (витрины ClickHouse) остаются, и отчёт для матча можно
сгенерировать заново — `python -m reportgen --match ID`. Удаляется
материализованный JSON, а не история матчей.

Безопасность по умолчанию:
  - `REPORTS_RETENTION_DAYS` не задан → чистка ВЫКЛЮЧЕНА;
  - CLI по умолчанию в режиме сухого прогона: удаление требует `--apply`.
Оба решения намеренные. Retention удаляет данные владельца, и цена
ошибки в конфиге («180» вместо «1800») несимметрична: лишний день
хранения стоит килобайт, лишнее удаление — часов пересчёта.

CLI:
    python -m reportgen.retention                 # что удалилось бы
    python -m reportgen.retention --days 180 --apply
"""
from __future__ import annotations

import argparse
import logging
import os

logger = logging.getLogger("reportgen.retention")

DAYS_ENV = "REPORTS_RETENTION_DAYS"

# Минимально допустимый срок. Значение вроде 1 почти наверняка опечатка
# (хотели 100), а последствия — вычищенная база отчётов. Ниже порога
# работа прерывается с явной ошибкой, а не выполняется молча.
MIN_DAYS = 7


def configured_days() -> int:
    """Срок хранения из окружения; 0 — retention выключен."""
    raw = os.getenv(DAYS_ENV, "").strip()
    if not raw:
        return 0
    try:
        days = int(raw)
    except ValueError:
        logger.warning("%s=%r — не число, retention выключен", DAYS_ENV, raw)
        return 0
    if days <= 0:
        return 0
    if days < MIN_DAYS:
        raise ValueError(
            f"{DAYS_ENV}={days}: срок меньше {MIN_DAYS} дней почти всегда "
            "опечатка; если это намеренно, задайте MIN_DAYS явно в коде")
    return days


def count_expired(conn, days: int) -> int:
    """Сколько отчётов старше `days` (ничего не удаляет)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM MatchReports"
            " WHERE generated_at < now() - make_interval(days => %s)", (days,))
        return int(cur.fetchone()[0])


def purge(conn, days: int, apply: bool = False) -> int:
    """Удалить отчёты старше `days`. Возвращает число затронутых строк.

    apply=False — сухой прогон: считаем, но не удаляем. Это дефолт, а не
    опция «на всякий случай»: посмотреть на число перед удалением должно
    быть проще, чем удалить.
    """
    if days <= 0:
        return 0
    n = count_expired(conn, days)
    if not apply:
        logger.info("сухой прогон: под удаление попало бы %d отчётов "
                    "старше %d дней (запуск с --apply удалит)", n, days)
        return n
    if n == 0:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM MatchReports"
            " WHERE generated_at < now() - make_interval(days => %s)", (days,))
        deleted = cur.rowcount
    logger.info("retention: удалено %d отчётов старше %d дней "
                "(данные матчей в ClickHouse не тронуты, отчёт "
                "пересоздаётся по запросу)", deleted, days)
    return deleted


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help=f"срок хранения (по умолчанию из {DAYS_ENV})")
    ap.add_argument("--apply", action="store_true",
                    help="действительно удалить (без флага — сухой прогон)")
    args = ap.parse_args()

    import psycopg

    days = args.days if args.days is not None else configured_days()
    if days <= 0:
        print(f"retention выключен: задайте {DAYS_ENV} или --days")
        return 0
    if days < MIN_DAYS:
        print(f"срок {days} дн. меньше минимума {MIN_DAYS} — отказ")
        return 1

    dsn = os.getenv("POSTGRES_DSN",
                    "postgresql://dota:dota_dev_password@localhost:5432/manta")
    with psycopg.connect(dsn, autocommit=True) as conn:
        n = purge(conn, days, apply=args.apply)
    print(f"{'удалено' if args.apply else 'попало бы под удаление'}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
