"""Тесты досчёта farm_core на старых матчах (спринт 140).

Проверяется главным образом то, чего инструмент НЕ должен делать. Он
трогает живую витрину, а самая дорогая ошибка здесь — не «не досчитал», а
«снёс то, что уже не восстановить»: смерти, варды и смоки старых матчей
пересчитать нечем, их сырьё истекло по TTL (миграция 007).
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "libs"))

import backfill_farm_core as bf  # noqa: E402


class FakeCH:
    """Подставной ClickHouse: помнит, что у него спрашивали и что писали.

    Очередь отдаётся отдельным полем, а не таблицей: запрос очереди тоже
    упоминает PositionSnapshots, и подбор ответа по имени таблицы вернул
    бы на него позиции — то есть строки без match_id.
    """

    def __init__(self, pending=None, **tables):
        self.tables = tables
        self.pending = pending or []
        self.inserted: list[tuple[str, list[dict]]] = []
        self.executed: list[str] = []
        self.queries: list[str] = []

    def select(self, query, params=None):
        self.queries.append(query)
        if "MatchMapCells" in query:      # запрос очереди
            return self.pending
        for name, rows in self.tables.items():
            if name in query:
                return rows
        return []

    def insert_rows(self, table, rows):
        self.inserted.append((table, [dict(r) for r in rows]))

    def execute(self, query, params=None):
        self.executed.append(query)


ROSTER = [
    {"player_id": 0, "team": 2, "hero": "npc_dota_hero_carry"},
    {"player_id": 1, "team": 2, "hero": "npc_dota_hero_mid"},
    {"player_id": 2, "team": 2, "hero": "npc_dota_hero_off"},
    {"player_id": 3, "team": 2, "hero": "npc_dota_hero_supp4"},
    {"player_id": 4, "team": 2, "hero": "npc_dota_hero_supp5"},
    {"player_id": 5, "team": 3, "hero": "npc_dota_hero_enemy"},
]
ECONOMY = [
    {"player_id": 0, "game_time": 1800, "lh": 400},
    {"player_id": 1, "game_time": 1800, "lh": 300},
    {"player_id": 2, "game_time": 1800, "lh": 200},
    {"player_id": 3, "game_time": 1800, "lh": 20},
    {"player_id": 4, "game_time": 1800, "lh": 10},
    {"player_id": 5, "game_time": 1800, "lh": 350},
]


def _pos(hero, x, y, t=60):
    return {"game_time": t, "hero": hero, "x": x, "y": y, "is_alive": 1}


POSITIONS = [
    _pos("npc_dota_hero_carry", -6000, -6000),
    _pos("npc_dota_hero_supp4", -5000, -5000),
    _pos("npc_dota_hero_enemy", 6000, 6000),
]


def _ch(pending=None, **over):
    return FakeCH(pending=pending,
                  **{"PlayerMatchFeatures": ROSTER,
                     "EconomyTimeline": ECONOMY,
                     "PositionSnapshots": POSITIONS, **over})


# -- чего инструмент не должен делать -------------------------------------------

def test_writes_only_farm_core_rows():
    """Ни одной строки другого вида.

    Инструмент считает через тот же build_cells, что и раннер, а тот
    попутно выдаёт presence и farm. Отдать их вместе с farm_core значило
    бы удвоить уже посчитанное присутствие — карта стала бы вдвое
    плотнее без единого нового факта.
    """
    rows = bf.farm_core_rows(_ch(), 42)
    assert rows, "на этих данных farm_core обязан посчитаться"
    assert {r["kind"] for r in rows} == {"farm_core"}


def test_never_deletes_anything():
    """Никаких DELETE и ALTER.

    Соблазн переиспользовать _replace_rows раннера был, и он сносит ВСЕ
    строки матча. Смерти, варды и смоки старых матчей пересчитать нечем —
    их сырьё истекло по TTL, и «обновление» стёрло бы данные навсегда.
    """
    ch = _ch()
    bf.farm_core_rows(ch, 42)
    assert ch.executed == []
    assert not any("DELETE" in q or "ALTER" in q for q in ch.queries)


def test_only_cores_are_counted():
    """Саппорт в безопасной зоне в farm_core не попадает.

    Смотрим на сторону Radiant: там стоят кор и саппорт, в РАЗНЫХ клетках
    сетки 32×32. Клетка должна остаться одна — саппортовой быть не
    должно. У Dire в ростере один игрок, и он тоже кор (троих там просто
    неоткуда взять) — его строка законна и проверке не мешает.
    """
    rows = bf.farm_core_rows(_ch(), 42)
    radiant = [r for r in rows if r["team"] == 2]
    assert len(radiant) == 1
    assert radiant[0]["n"] == 1


def test_match_id_is_stamped():
    """Без match_id строки лягут в чужой матч — точнее, в матч 0."""
    rows = bf.farm_core_rows(_ch(), 4242)
    assert all(r["match_id"] == 4242 for r in rows)


# -- когда считать нечем --------------------------------------------------------

@pytest.mark.parametrize("missing", ["PlayerMatchFeatures", "PositionSnapshots"])
def test_missing_source_yields_nothing_not_garbage(missing):
    """Нет ростера или позиций — пусто, а не строки с пустыми героями."""
    assert bf.farm_core_rows(_ch(**{missing: []}), 42) == []


def test_no_economy_means_no_rows():
    """Без добиток ранжировать некого, и вид не пишется ВОВСЕ.

    Посчитанный по всем пятерым, он был бы неотличим от честного и врал
    бы молча — та же логика, что в раннере.
    """
    assert bf.farm_core_rows(_ch(EconomyTimeline=[]), 42) == []


# -- выборка очереди ------------------------------------------------------------

def test_pending_query_uses_final_and_filters_by_kind():
    """FINAL обязателен, а вид проверяется параметром, а не строкой.

    MatchMapCells — ReplacingMergeTree: без FINAL уже досчитанный матч
    попадал бы в выборку повторно из-за несмёрженных кусков, и счётчики
    прогона врали бы про остаток работы.
    """
    ch = _ch(pending=[{"match_id": 7}, {"match_id": 9}])
    assert bf.pending_matches(ch, 10) == [7, 9]
    q = ch.queries[-1]
    assert "FINAL" in q
    assert "{kind:String}" in q and "{limit:UInt32}" in q


# -- прогон целиком -------------------------------------------------------------

def test_run_never_deletes_and_writes_only_map_cells():
    """САМОЕ важное свойство инструмента, и проверять его надо на прогоне.

    Пока запись жила внутри main() с настоящим клиентом, мутация,
    добавляющая DELETE прямо перед вставкой, не роняла ни одного теста:
    проверка «ничего не удаляет» смотрела только на сборщик строк.
    """
    ch = _ch(pending=[{"match_id": 7}])
    bf.run(ch, limit=5, dry_run=False)
    assert ch.executed == [], "инструмент выполнил DDL/DML — он не должен"
    assert all(t == "MatchMapCells" for t, _ in ch.inserted)
    for _, rows in ch.inserted:
        assert {r["kind"] for r in rows} == {"farm_core"}


def test_dry_run_writes_nothing():
    """Сухой прогон обязан быть по-настоящему сухим: на нём и решают,
    запускать ли настоящий."""
    ch = _ch(pending=[{"match_id": 7}])
    bf.run(ch, limit=5, dry_run=True)
    assert ch.inserted == []
    assert ch.executed == []


def test_empty_queue_is_success_not_failure():
    """Нечего досчитывать — это норма, а не сбой.

    Иначе повторный запуск после успешного прогона возвращал бы
    ненулевой код, и в cron это читалось бы как поломка.
    """
    ch = FakeCH()
    assert bf.run(ch, limit=5, dry_run=False) == 0


def test_totally_failed_run_is_not_reported_as_success():
    """Очередь есть, а досчитать не удалось ничего — ненулевой код.

    Молчащий успех на полностью провалившемся прогоне — худший вид
    отчёта: работа не сделана, а выглядит сделанной.
    """
    ch = _ch(pending=[{"match_id": 7}], PlayerMatchFeatures=[])
    assert bf.run(ch, limit=5, dry_run=False) == 1
    assert ch.inserted == []


def test_unusable_match_does_not_read_positions():
    """Без ростера позиции не читаются ВОВСЕ.

    PositionSnapshots — самая тяжёлая таблица в базе, а бэкфилл идёт по
    тысячам матчей. Прочитать её ради матча, который всё равно нечем
    считать, — не ошибка результата, но ровно то, из-за чего прогон
    вместо часа идёт ночь.
    """
    ch = _ch(PlayerMatchFeatures=[])
    ch.queries.clear()
    assert bf.farm_core_rows(ch, 42) == []
    assert not any("PositionSnapshots" in q for q in ch.queries)


def test_match_without_last_hits_does_not_read_positions():
    """То же для матча, у которого нет добиток: коров не определить."""
    ch = _ch(EconomyTimeline=[])
    ch.queries.clear()
    assert bf.farm_core_rows(ch, 42) == []
    assert not any("PositionSnapshots" in q for q in ch.queries)


def test_skip_reason_distinguishes_missing_roster_from_missing_economy(caplog):
    """Причина пропуска называется, а не сводится к «пропущено».

    Бэкфилл идёт по тысячам матчей и молча пропускает часть. Разница
    между «нет ростера» и «нет добиток» — это разница между сломанной
    витриной фич и сломанным сбором экономики, то есть между двумя
    разными починками. Сводить их в один счётчик значит превратить
    диагноз в загадку.
    """
    import logging
    with caplog.at_level(logging.INFO, logger="farm-core-backfill"):
        bf.farm_core_rows(_ch(PlayerMatchFeatures=[]), 1)
        no_roster = caplog.text
        caplog.clear()
        bf.farm_core_rows(_ch(EconomyTimeline=[]), 2)
        no_economy = caplog.text
    assert "ростер" in no_roster
    assert "ростер" not in no_economy
    assert "кор" in no_economy
