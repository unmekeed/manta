"""Раннер ПИШЕТ поминутный агрегат, а не только умеет его считать (147).

ЗАЧЕМ ОТДЕЛЬНЫЙ ТЕСТ. Функция может быть безупречной и не вызываться —
это самая дорогая дыра нынешнего развёртывания: дважды подряд (профили
коллекторов в 143, проверка живости там же) проверка требовала, чтобы код
УПОМИНАЛ нужное, а не делал. Здесь process_match гоняется целиком на
поддельном ClickHouse, и проверяется, что в него ушли обе таблицы.

Заодно это единственное место, где согласие двух зернистостей проверяется
НА ВЫХОДЕ раннера, а не на выходе чистой функции: между ними стоит ещё
простановка match_id, и перепутать списки там ничего не стоит.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "libs"))

from extractor.runner import Extractor  # noqa: E402

MATCH_ID = 4242
RADIANT = ["npc_dota_hero_axe", "npc_dota_hero_lina", "npc_dota_hero_juggernaut",
           "npc_dota_hero_lion", "npc_dota_hero_pudge"]
DIRE = ["npc_dota_hero_sven", "npc_dota_hero_zuus", "npc_dota_hero_tiny",
        "npc_dota_hero_cm", "npc_dota_hero_bane"]
DURATION = 1800


def _players():
    return ([{"team": 2, "hero": h, "name": f"r{i}"}
             for i, h in enumerate(RADIANT)]
            + [{"team": 3, "hero": h, "name": f"d{i}"}
               for i, h in enumerate(DIRE)])


def _economy():
    """Десять игроков, добитки растут — значит FarmClock видит фарм."""
    rows = []
    for pid in range(10):
        for step, t in enumerate(range(0, DURATION, 60)):
            rows.append({"player_id": pid, "game_time": t,
                         "net_worth": 600 + step * 100,
                         "total_gold": 600 + step * 100,
                         "total_xp": step * 90,
                         "lh": step * 2, "dn": step})
    return rows


def _positions():
    """Герои расходятся по карте, так что клетки заведомо разные."""
    rows = []
    heroes = RADIANT + DIRE
    for i, hero in enumerate(heroes):
        for step, t in enumerate(range(0, DURATION, 60)):
            rows.append({"game_time": t, "hero": hero,
                         "x": -7000 + i * 1400 + step * 20,
                         "y": -7000 + step * 400,
                         "is_alive": 1})
    return rows


class FakeCH:
    """ClickHouse, отвечающий по тексту запроса.

    `existing` — сколько строк агрегата якобы уже лежит. Ноль означал бы,
    что ветка удаления не выполняется вовсе: `_replace_rows` чистит
    таблицу только при непустом счётчике. Первая версия этого теста
    отвечала нулём и объявляла отсутствие DELETE поломкой кода — тогда
    как поломан был тест.
    """

    def __init__(self, existing: int = 5):
        self.existing = existing
        self.inserted: dict[str, list[dict]] = {}
        self.deleted: list[str] = []

    def select(self, query, params=None):
        if "FROM EconomyTimeline" in query:
            return _economy()
        if "FROM PositionSnapshots" in query:
            return _positions()
        if "count()" in query:
            return [{"n": self.existing}]
        if "FROM ReplayEvents" in query:
            return []          # событий нет: карту держат позиции
        return []

    def execute(self, query, params=None):
        self.deleted.append(query)

    def insert_rows(self, table, rows):
        self.inserted.setdefault(table, []).extend(list(rows))


class FakeProducer:
    def produce(self, *a, **kw):
        pass

    def flush(self, *a, **kw):
        return 0


def _run() -> FakeCH:
    ch = FakeCH()
    ex = Extractor.__new__(Extractor)
    ex.ch = ch
    ex.producer = FakeProducer()
    ex._fs_stub = None
    ex.cfg = None
    # Онлайн-слой — кэш; в этом тесте он не при чём, и настоящий поход в
    # feature-store увёл бы проверку в сеть.
    ex._push_online = lambda *a, **kw: None
    ex.process_match(MATCH_ID, _players(), "Radiant", DURATION, None)
    return ch


def test_both_map_tables_are_written():
    """Главное: поминутная таблица не забыта."""
    ch = _run()
    assert "MatchMapCellsMinute" in ch.inserted, \
        "поминутный агрегат посчитан, но не записан"
    assert "MatchMapCells" in ch.inserted, "фазовый агрегат пропал"
    assert ch.inserted["MatchMapCellsMinute"], "поминутных строк ноль"


def test_written_rows_carry_the_match_id():
    """match_id проставляется ОБОИМ спискам.

    Списка два, а цикл простановки один на каждый — забыть второй легко,
    и строки уйдут в ClickHouse без ключа партиционирования.
    """
    ch = _run()
    for table in ("MatchMapCellsMinute", "MatchMapCells"):
        assert all(r.get("match_id") == MATCH_ID for r in ch.inserted[table]), \
            f"{table}: строки без match_id"


def test_phase_rows_are_the_sum_of_the_minute_rows_on_the_way_out():
    """Согласие проверяется на ВЫХОДЕ раннера, а не чистой функции."""
    ch = _run()
    minutes = ch.inserted["MatchMapCellsMinute"]
    phases = ch.inserted["MatchMapCells"]
    assert sum(r["n"] for r in minutes) == sum(r["n"] for r in phases)
    for kind in {r["kind"] for r in minutes}:
        a = sum(r["n"] for r in minutes if r["kind"] == kind)
        b = sum(r["n"] for r in phases if r["kind"] == kind)
        assert a == b, f"{kind}: поминутно {a}, по фазам {b}"


def test_minutes_actually_vary():
    """Проверка от вырождения: minute — не константа.

    Начни она вычисляться как ноль, суммы всё равно сойдутся (ни одного
    попадания не потеряно), и проверка согласия осталась бы зелёной.
    Различает их только то, что минут действительно много.

    Сравнивать РАЗМЕРЫ таблиц для этого нельзя: во сколько раз поминутная
    длиннее фазовой, зависит от того, как быстро герои двигаются, — на
    синтетике первой версии теста вышло всего 1.2 раза, и порог «вдвое»
    объявил бы поломкой верный код.
    """
    ch = _run()
    minutes = {r["minute"] for r in ch.inserted["MatchMapCellsMinute"]}
    assert len(minutes) == DURATION // 60, (
        f"ожидались минуты 0..{DURATION // 60 - 1}, а различных всего "
        f"{len(minutes)}")
    assert min(minutes) == 0 and max(minutes) == DURATION // 60 - 1


def test_old_rows_are_deleted_before_insert_for_both_tables():
    """Переразбор ЗАМЕЩАЕТ обе таблицы.

    ReplacingMergeTree схлопывает только строки с тем же ключом, а после
    правки формулы ключ меняется — старые строки остаются рядом
    (инцидент 2026-08-05, 8764 строки-призрака). Новая таблица обязана
    получить ту же защиту, иначе она заведёт такой же долг с нуля.
    """
    ch = _run()
    assert any("MatchMapCellsMinute" in q for q in ch.deleted), \
        "поминутные строки не удаляются перед вставкой"
    assert any("DELETE FROM MatchMapCells " in q or
               "DELETE FROM MatchMapCells\n" in q for q in ch.deleted)
