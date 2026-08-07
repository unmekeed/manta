"""Тесты суточного бюджета вызовов OpenDota (спринт 130).

Проверяется то, из-за чего спринт и появился: 2026-08-06 один источник
выел общую квоту 2000/сутки и остановил ВСЕ коллекторы на шесть часов,
включая кормящие про-эталон промоушен-гейта.

Ключевое требование — исчерпание бюджета должно БРОСАТЬ, а не возвращать
False: возвращаемое значение слишком легко проигнорировать, и мы уже
дважды на этом обожглись (спринты 126.1 и 129.1, где исчерпанная квота
трактовалась как свойство отдельного матча).
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector import budget  # noqa: E402


class FakeCursor:
    """Курсор поверх словаря — та же семантика, что у UPSERT ... RETURNING."""

    def __init__(self, store):
        self.store = store
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        if sql.startswith("UPDATE"):
            n, day, api, source = params
            key = (day, api, source)
            self.store[key] = self.store.get(key, 0) - n
            self._rows = None
        elif sql.startswith("INSERT"):
            day, api, source, n = params
            key = (day, api, source)
            self.store[key] = self.store.get(key, 0) + n
            self._last = (self.store[key],)
            self._rows = None
        elif "SELECT source, calls" in sql:
            day, api = params
            self._rows = [(src, n) for (d, a, src), n in self.store.items()
                          if d == day and a == api]
        else:
            day, api, source = params
            self._last = (self.store.get((day, api, source), 0),)
            self._rows = None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._rows or []


class FakeDB:
    def __init__(self):
        self.store = {}

    def cursor(self):
        return FakeCursor(self.store)

    def close(self):
        pass


def _budget(limit, source="candidates", shares=None, global_limit=10_000,
            db=None):
    """Бюджет поверх фейковой БД.

    global_limit по умолчанию заведомо большой: тесты про ГАРАНТИЮ не
    должны случайно упираться в общий пул, иначе они проверяли бы не то,
    что написано в их названии.
    """
    b = budget.ApiBudget.__new__(budget.ApiBudget)
    b._dsn = "fake"
    b._db = db or FakeDB()
    b._source = source
    b._limit = limit
    b._api = "opendota"
    b._shares = dict(shares if shares is not None else {source: limit})
    b._global = global_limit
    return b


def test_spend_counts_and_allows_within_limit():
    b = _budget(3)
    assert [b.spend() for _ in range(3)] == [1, 2, 3]
    assert b.used() == 3


def test_spend_raises_when_the_shared_pool_is_out():
    """Бросает, а не возвращает False — иначе вызывающий это проигнорирует.

    Потолок теперь общий: упереться можно только в исчерпанный пул, а не
    в собственную долю (см. test_borrows_idle_share_late_in_the_day).
    """
    b = _budget(2, global_limit=2)
    b.spend()
    b.spend()
    with pytest.raises(budget.BudgetExhausted):
        b.spend()


def test_exhausted_message_names_the_source_and_numbers():
    b = _budget(1, source="opendota-timeline", global_limit=1)
    b.spend()
    with pytest.raises(budget.BudgetExhausted, match="opendota-timeline"):
        b.spend()


def test_guarantee_survives_a_greedy_neighbour():
    """Смысл долей: сосед-обжора не может отобрать чужую гарантию.

    Инцидент 2026-08-06 был ровно такой — кандидаты выели общую квоту и
    остановили источники, кормящие про-эталон промоушен-гейта.
    """
    shares = {"candidates": 5, "opendota-timeline-pro": 3}
    db = FakeDB()
    greedy = _budget(5, "candidates", shares, global_limit=8, db=db)
    pro = _budget(3, "opendota-timeline-pro", shares, global_limit=8, db=db)
    greedy._day_left = pro._day_left = lambda: 1.0    # начало суток

    for _ in range(5):
        greedy.spend()
    with pytest.raises(budget.BudgetExhausted):
        greedy.spend()          # своя доля выбрана, чужая зарезервирована

    for i in range(3):
        assert pro.spend() == i + 1, "гарантия соседа была отдана обжоре"


def test_guarantee_erodes_only_as_the_day_runs_out():
    """Честная граница правила, а не оговорка мелким шрифтом.

    Резерв под чужую невыбранную долю тает вместе с сутками, поэтому к
    вечеру сосед вправе занять её часть — и проснувшийся позже получит
    меньше номинала. Это осознанный обмен: держать долю молчащего
    источника до полуночи значит гарантированно её потерять.
    """
    shares = {"candidates": 5, "opendota-timeline-pro": 3}
    db = FakeDB()
    greedy = _budget(5, "candidates", shares, global_limit=8, db=db)
    greedy._day_left = lambda: 0.0        # сутки кончаются, сосед молчал
    for _ in range(8):
        greedy.spend()
    assert greedy.used() == 8, "невыбранная доля пропала зря"
    with pytest.raises(budget.BudgetExhausted):
        greedy.spend()                    # но пул — стена в любом случае


def test_borrows_idle_share_late_in_the_day():
    """Главная поправка спринта 135.

    Замер 2026-08-07 08:24 UTC: `stratz-timeline` (53/50) и
    `opendota-timeline` (253/250) спали до полуночи, выбрав свои
    крошечные доли, — при том что общая квота была цела на 85%, а приток
    из всех источников, кроме реплейного, стоял с трёх ночи. Жёсткая доля
    оказалась хуже болезни, которую лечила.
    """
    shares = {"stratz-timeline": 2, "candidates": 8}
    b = _budget(2, "stratz-timeline", shares, global_limit=10)
    b._day_left = lambda: 0.1          # сутки на исходе, сосед молчит
    for _ in range(5):
        b.spend()
    assert b.used() == 5, "доля не одолжена при свободном пуле"


def test_does_not_borrow_at_the_start_of_the_day():
    """На рассвете одалживать нельзя: молчащий сосед ещё проснётся.

    Без этого кандидаты за первый час съели бы доли всех остальных, и
    инцидент 2026-08-06 повторился бы в другой обёртке.
    """
    shares = {"stratz-timeline": 2, "candidates": 8}
    b = _budget(2, "stratz-timeline", shares, global_limit=10)
    b._day_left = lambda: 1.0
    b.spend()
    b.spend()
    with pytest.raises(budget.BudgetExhausted):
        b.spend()


def test_pool_is_never_exceeded_even_when_everyone_borrows():
    """Заимствование не должно пробивать общий потолок — иначе мы просто
    вернулись к «кто успел», только с более сложным кодом."""
    shares = {"a": 5, "b": 5}
    db = FakeDB()
    a = _budget(5, "a", shares, global_limit=10, db=db)
    b = _budget(5, "b", shares, global_limit=10, db=db)
    a._day_left = b._day_left = lambda: 0.0

    spent = 0
    for src in (a, b, a, b, a, b, a, b, a, b, a, b):
        try:
            src.spend()
            spent += 1
        except budget.BudgetExhausted:
            pass
    assert spent == 10, spent


def test_shares_fit_into_the_pool():
    """Сумма гарантий не может превышать пул: иначе гарантия перестаёт
    быть гарантией, и какой-то источник узнает об этом в проде."""
    assert sum(budget.SHARES.values()) <= budget.GLOBAL_LIMIT


def test_pool_leaves_headroom_under_the_real_quota():
    """Проверка и инкремент — два запроса, поэтому несколько процессов
    могут слегка перелететь. Запас до настоящей квоты обязан это
    покрывать."""
    assert budget.GLOBAL_LIMIT < budget.OPENDOTA_DAILY_LIMIT


def test_zero_limit_disables_accounting_without_raising():
    """Не настроен — no-op: забытая настройка не должна ронять сбор."""
    b = _budget(0)
    assert b.spend() == 0
    assert b.spend() == 0


def test_module_spend_is_noop_until_configured():
    budget.reset_for_tests()
    budget.spend()          # не должно бросать и не должно ходить в БД
    budget.spend(5)


def test_module_spend_uses_configured_budget():
    budget.reset_for_tests()
    b = _budget(1, global_limit=1)
    budget._current = b
    try:
        budget.spend()
        with pytest.raises(budget.BudgetExhausted):
            budget.spend()
    finally:
        budget.reset_for_tests()


def test_day_key_is_utc_not_local(monkeypatch):
    """OpenDota сбрасывает счётчик в 00:00 UTC.

    По московскому календарю бюджет обнулялся бы на три часа раньше — и
    эти три часа мы получали бы 429 на ровном месте.

    Часы подменяются так, чтобы локальная дата и UTC-дата РАЗЛИЧАЛИСЬ:
    иначе тест зелен на машине в UTC (наш контейнер — как раз такая) при
    любом поведении кода и не проверяет ничего.
    """
    from datetime import datetime, timedelta, timezone

    utc_moment = datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # tz=None — «локальное» время, здесь UTC+3: уже 7 августа.
            return utc_moment if tz else (utc_moment.replace(tzinfo=None)
                                          + timedelta(hours=3))

    monkeypatch.setattr(budget, "datetime", FakeDatetime)
    assert _budget(1)._today() == "2026-08-06"


def test_rejected_spend_does_not_consume_the_budget():
    """Отказ не должен стоить вызова: коллектор в этот момент никуда не ходил.

    Найдено тестом на гарантию соседа. Инкремент идёт перед проверкой, и
    без отката счётчик рос от каждой ОТВЕРГНУТОЙ попытки — источник в
    цикле повторов раздувал и свою строку, и общий пул. Это и объясняет
    полевое расхождение 2026-08-07: ApiBudget насчитал 1304 вызова за
    сутки, сама OpenDota — 304.
    """
    b = _budget(2, global_limit=2)
    b.spend()
    b.spend()
    for _ in range(5):
        with pytest.raises(budget.BudgetExhausted):
            b.spend()
    assert b.used() == 2, "отвергнутые попытки списали квоту"


def test_day_left_shrinks_through_the_day_and_is_utc(monkeypatch):
    """Резерв под чужие доли тает пропорционально остатку суток.

    Проверяется настоящая реализация, а не подменённая: остальные тесты
    заимствования подставляют _day_left вручную, и без этого теста
    мутация «день не кончается никогда» проходила незамеченной —
    заимствование не включилось бы ни разу, а бюджет вёл бы себя как
    жёсткая доля, ради отмены которой спринт и затевался.

    Часы подменяются так, чтобы локальная дата и UTC РАЗЛИЧАЛИСЬ: иначе
    тест зелен на машине в UTC при любом поведении кода.
    """
    from datetime import datetime, timedelta, timezone

    def at(hour):
        moment = datetime(2026, 8, 7, hour, 0, tzinfo=timezone.utc)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                # tz=None — «локальное» время, здесь UTC+3.
                return moment if tz else (moment.replace(tzinfo=None)
                                          + timedelta(hours=3))

        monkeypatch.setattr(budget, "datetime", FakeDatetime)
        return _budget(1)._day_left()

    assert at(0) == 1.0
    assert abs(at(12) - 0.5) < 1e-9
    assert abs(at(18) - 0.25) < 1e-9, "считается по локальным часам, не UTC"
