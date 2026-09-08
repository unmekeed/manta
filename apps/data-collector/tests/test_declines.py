"""Общая память источников об отказах (спринт 204).

Проверяются решения, а не SQL: сам запрос гоняется PREPARE на настоящей
схеме (scripts/tests/test_sql_prepares.py), а здесь — то, что легко
решить наоборот.

ГЛАВНОЕ СВОЙСТВО: отказ записи НЕ ОСТАНАВЛИВАЕТ СБОР. Общая память —
ускоритель, а не условие работы. Недоступная база означает, что напарник
не узнает про отвергнутый матч, то есть ровно то поведение, что было до
этого спринта. Ронять из-за неё цикл значило бы менять «собираем
медленнее» на «не собираем вовсе».
"""
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector.declines import (RECENT_SQL, UPSERT_SQL,  # noqa: E402
                                declined_by, prune, record)


class FakeCursor:
    """Курсор, повторяющий контракт psycopg настолько, насколько нужно.

    Заглушка, устроенная проще боевого клиента, проверяет несуществующую
    систему — правило, которое этот проект оплатил трижды. Поэтому здесь
    есть и контекстный менеджер, и `rowcount`, и `fetchall`.
    """

    def __init__(self, rows=(), fail=None):
        self.rows = list(rows)
        self.fail = fail
        self.executed = []
        self.rowcount = len(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.fail:
            raise self.fail
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeDB:
    def __init__(self, rows=(), fail=None):
        self._cur = FakeCursor(rows, fail)
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_a_decline_is_written_with_its_reason():
    """Причина отказа записывается, а не теряется.

    Отказы бывают разной природы: «нет матча» у STRATZ временно и
    лечится ожиданием, «фильтр» постоянен и напарнику ничего не сулит.
    Без причины таблица отвечала бы на «сколько», не отвечая на
    «почему», — и первый же разбор начался бы с логов.
    """
    db = FakeDB()
    assert record(db, "stratz_timeline", 42, "нет матча", attempts=3)
    sql, params = db._cur.executed[0]
    assert params["reason"] == "нет матча"
    assert params["attempts"] == 3
    assert db.commits == 1


def test_a_write_failure_does_not_raise():
    """ГЛАВНОЕ: недоступная база не роняет цикл сбора.

    Общая память — ускоритель. Её отказ означает возврат к поведению,
    которое было до спринта 204: напарник не узнает про матч. Исключение
    отсюда уронило бы цикл целиком, то есть остановило бы и ту половину
    сбора, которая работает.
    """
    db = FakeDB(fail=RuntimeError("postgres недоступен"))
    assert record(db, "stratz_timeline", 42, "нет матча") is False
    assert db.rollbacks == 1, "неудачная запись оставила транзакцию открытой"


def test_a_read_failure_gives_an_empty_set_not_an_exception():
    """Чтение отказов тоже не роняет цикл.

    Пустое множество здесь — «мы не знаем про отказы напарника», и
    делитель ведёт себя как до спринта. Исключение остановило бы сбор
    из-за вспомогательной таблицы.
    """
    db = FakeDB(fail=RuntimeError("postgres недоступен"))
    assert declined_by(db, "stratz_timeline") == set()


def test_the_read_is_windowed_by_time():
    """Спрашиваем свежие отказы, а не все за историю.

    Матч недельной давности уже уехал из окна листинга, и брать его
    сверх доли некому: напарнику он больше не встретится. Без окна
    множество росло бы вечно и однажды перестало помещаться в памяти —
    молча, потому что «просто много матчей» ни на что не похоже.
    """
    db = FakeDB(rows=[(1,), (2,)])
    assert declined_by(db, "stratz_timeline", hours=6) == {1, 2}
    _, params = db._cur.executed[0]
    assert params["hours"] == 6
    assert "declined_at >" in RECENT_SQL


def test_repeated_declines_add_up_instead_of_overwriting():
    """Повторный отказ по тому же матчу СУММИРУЕТ попытки.

    Матч, за который брались десять раз и не смогли, и матч, за который
    брались однажды, — разные утверждения. Затирание превратило бы
    историю попыток в моментальный снимок, а по нему нельзя отличить
    «STRATZ отстаёт на минуты» от «STRATZ этого матча не увидит никогда».
    """
    assert "attempts    = SourceDeclines.attempts + EXCLUDED.attempts" in \
        UPSERT_SQL, "повторный отказ затирает счётчик попыток"


def test_pruning_reports_how_much_it_removed():
    """Уборка возвращает число, а не молчит.

    Ноль удалённых при растущей таблице — это диагноз (окно не
    совпадает с тем, что пишется), и увидеть его можно только по числу.
    """
    db = FakeDB(rows=[(1,), (2,), (3,)])
    assert prune(db, hours=72) == 3


def test_every_named_parameter_of_the_upsert_is_supplied():
    """Ключи параметров совпадают с тем, что подставляет `record`.

    Разъезд здесь не падает при разработке: psycopg сообщит о
    недостающем параметре только на живой базе, то есть в production.
    """
    db = FakeDB()
    record(db, "stratz_timeline", 42, "нет матча", 2)
    _, params = db._cur.executed[0]
    named = {p.split(")")[0] for p in UPSERT_SQL.split("%(")[1:]}
    assert named == set(params)


# -- проводка: источник действительно зовёт напарника ---------------------------

def stratz(on_decline, attempts=3):
    """STRATZ с подставным токеном: строится без сети."""
    from collector.sources.stratz import StratzTimelineSource

    return StratzTimelineSource(token="dummy", retry_attempts=attempts,
                                on_decline=on_decline)


def test_the_partner_is_told_only_when_the_attempts_run_out():
    """ГЛАВНОЕ ПРО ПРОВОДКУ: сообщаем при ИСЧЕРПАНИИ попыток, не раньше.

    Пока матч в `_pending`, мы ещё собираемся его взять. Отдать его при
    первой неудаче значило бы завести драку за один матч — ровно то,
    ради предотвращения чего деление и заведено.

    Проверяется ВЫЗОВ, а не наличие кода: в спринте 195 функция
    `_announce` существовала и была написана верно, но `collect_once` её
    не вызывал, и ни один тест этого не замечал.
    """
    told = []
    src = stratz(lambda mid, kind, n: told.append((mid, kind, n)), attempts=3)

    assert src.defer_match(42) is False and told == [], (
        "напарнику отдали матч, за который мы ещё собираемся взяться")
    assert src.defer_match(42) is False and told == []
    assert src.defer_match(42) is True, "попытки исчерпаны, а мы не сдались"
    assert told == [(42, "нет матча", 3)], told


def test_a_declined_match_is_not_retried_by_us():
    """Сдавшись, сами больше не пробуем.

    Иначе матч достался бы обоим: напарник берёт его по общей памяти, а
    мы продолжаем спрашивать — то есть платим за то, что уже отдали.
    """
    src = stratz(None)
    for _ in range(3):
        src.defer_match(7)
    assert 7 in src._rejected
    assert 7 not in src._pending


def test_a_failing_partner_callback_does_not_break_the_cycle():
    """Ошибка записи отказа не роняет сбор.

    Общая память — ускоритель, а не условие работы. Исключение отсюда
    остановило бы цикл целиком, то есть и ту часть сбора, которая
    исправна.
    """
    def broken(mid, kind, n):
        raise RuntimeError("postgres недоступен")

    src = stratz(broken)
    for _ in range(2):
        src.defer_match(9)
    assert src.defer_match(9) is True, "сбой записи отменил отказ"
    assert 9 in src._rejected


def test_the_reason_reaches_the_partner():
    """Причина отказа передаётся, а не подменяется умолчанием.

    «Нет матча» и «нет рядов» — разные утверждения: первое отступом не
    лечится вовсе, второе вопрос времени. Напарнику нужна та причина,
    которая была на самом деле.
    """
    told = []
    src = stratz(lambda mid, kind, n: told.append(kind), attempts=1)
    src.defer_match(5, "нет рядов")
    assert told == ["нет рядов"], told
