"""Тесты пересчёта карт фарма на старых матчах (спринты 140–141).

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

    def __init__(self, pending=None, existing=None, **tables):
        self.tables = tables
        self.pending = pending or []
        # Уже лежащие клетки фарма — отдельно от очереди: оба запроса
        # упоминают MatchMapCells, и различает их только форма.
        self.existing = existing or []
        self.inserted: list[tuple[str, list[dict]]] = []
        self.executed: list[str] = []
        self.queries: list[str] = []

    def select(self, query, params=None):
        self.queries.append(query)
        if "SELECT phase, team, kind" in query:   # уже лежащие клетки
            return self.existing
        if "MatchMapCells" in query:               # запрос очереди
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
# По ДВА сэмпла на игрока: со спринта 141 фарм определяется ростом
# счётчика добиток между сэмплами, и по одной точке его не видно —
# ранжировать коров можно, а сказать «фармил в этот интервал» нельзя.
ECONOMY = [
    r for pid, lh in [(0, 400), (1, 300), (2, 200), (3, 20), (4, 10), (5, 350)]
    for r in ({"player_id": pid, "game_time": 60, "lh": lh // 2},
              {"player_id": pid, "game_time": 1800, "lh": lh})
]


def _pos(hero, x, y, t=60):
    return {"game_time": t, "hero": hero, "x": x, "y": y, "is_alive": 1}


POSITIONS = [
    _pos("npc_dota_hero_carry", -6000, -6000),
    _pos("npc_dota_hero_supp4", -5000, -5000),
    _pos("npc_dota_hero_enemy", 6000, 6000),
]


CUTOFF = "2026-01-01 00:00:00"


def _ch(pending=None, **over):
    return FakeCH(pending=pending,
                  **{"PlayerMatchFeatures": ROSTER,
                     "EconomyTimeline": ECONOMY,
                     "PositionSnapshots": POSITIONS, **over})


# -- чего инструмент не должен делать -------------------------------------------

def test_writes_only_farm_kinds():
    """Ни одной строки чужого вида.

    Инструмент считает через тот же build_cells, что и раннер, а тот
    попутно выдаёт presence. Отдать его вместе с фармом значило бы
    удвоить уже посчитанное присутствие — карта стала бы вдвое плотнее
    без единого нового факта.
    """
    rows = bf.farm_rows(_ch(), 42)
    assert rows, "на этих данных фарм обязан посчитаться"
    assert {r["kind"] for r in rows} <= {"farm", "farm_core"}
    assert "presence" not in {r["kind"] for r in rows}


def test_never_deletes_anything():
    """Никаких DELETE и ALTER.

    Соблазн переиспользовать _replace_rows раннера был, и он сносит ВСЕ
    строки матча. Смерти, варды и смоки старых матчей пересчитать нечем —
    их сырьё истекло по TTL, и «обновление» стёрло бы данные навсегда.
    """
    ch = _ch()
    bf.farm_rows(ch, 42)
    assert ch.executed == []
    assert not any("DELETE" in q or "ALTER" in q for q in ch.queries)


def test_only_cores_are_counted():
    """Саппорт попадает в farm, но не в farm_core.

    Оба фармят (счётчик добиток растёт у обоих) и стоят в РАЗНЫХ клетках
    сетки 32×32, поэтому у Radiant должно выйти две клетки farm и одна
    farm_core.
    """
    rows = bf.farm_rows(_ch(), 42)
    radiant = [r for r in rows if r["team"] == 2]
    assert len([r for r in radiant if r["kind"] == "farm"]) == 2
    assert len([r for r in radiant if r["kind"] == "farm_core"]) == 1


def test_match_id_is_stamped():
    """Без match_id строки лягут в чужой матч — точнее, в матч 0."""
    rows = bf.farm_rows(_ch(), 4242)
    assert all(r["match_id"] == 4242 for r in rows)


# -- когда считать нечем --------------------------------------------------------

@pytest.mark.parametrize("missing", ["PlayerMatchFeatures", "PositionSnapshots"])
def test_missing_source_yields_nothing_not_garbage(missing):
    """Нет ростера или позиций — пусто, а не строки с пустыми героями."""
    assert bf.farm_rows(_ch(**{missing: []}), 42) == []


def test_no_economy_means_no_rows():
    """Без экономики матч ПРОПУСКАЕТСЯ, а не пересчитывается в пустоту.

    Со спринта 141 экономика нужна для самого определения фарма. Пустой
    пересчёт погасил бы нулями всё, что уже посчитано, — то есть стёр бы
    карту у матча, которому просто не хватило источника. Непересчитанный
    матч честнее обнулённого.
    """
    assert bf.farm_rows(_ch(EconomyTimeline=[]), 42) == []


# -- выборка очереди ------------------------------------------------------------

def test_pending_query_uses_final_and_filters_by_kind():
    """FINAL обязателен, виды и отметка идут параметрами, а не строкой.

    MatchMapCells — ReplacingMergeTree: без FINAL уже досчитанный матч
    попадал бы в выборку повторно из-за несмёрженных кусков, и счётчики
    прогона врали бы про остаток работы.
    """
    ch = _ch(pending=[{"match_id": 7}, {"match_id": 9}])
    assert bf.pending_matches(ch, 10, CUTOFF) == [7, 9]
    q = ch.queries[-1]
    assert "FINAL" in q
    assert "{kinds:Array(String)}" in q and "{limit:UInt32}" in q
    assert "{cutoff:DateTime}" in q


# -- прогон целиком -------------------------------------------------------------

def test_run_never_deletes_and_writes_only_map_cells():
    """САМОЕ важное свойство инструмента, и проверять его надо на прогоне.

    Пока запись жила внутри main() с настоящим клиентом, мутация,
    добавляющая DELETE прямо перед вставкой, не роняла ни одного теста:
    проверка «ничего не удаляет» смотрела только на сборщик строк.
    """
    ch = _ch(pending=[{"match_id": 7}])
    bf.run(ch, limit=5, dry_run=False, cutoff=CUTOFF)
    assert ch.executed == [], "инструмент выполнил DDL/DML — он не должен"
    assert all(t == "MatchMapCells" for t, _ in ch.inserted)
    for _, rows in ch.inserted:
        assert {r["kind"] for r in rows} <= {"farm", "farm_core"}


def test_dry_run_writes_nothing():
    """Сухой прогон обязан быть по-настоящему сухим: на нём и решают,
    запускать ли настоящий."""
    ch = _ch(pending=[{"match_id": 7}])
    bf.run(ch, limit=5, dry_run=True, cutoff=CUTOFF)
    assert ch.inserted == []
    assert ch.executed == []


def test_empty_queue_is_success_not_failure():
    """Нечего досчитывать — это норма, а не сбой.

    Иначе повторный запуск после успешного прогона возвращал бы
    ненулевой код, и в cron это читалось бы как поломка.
    """
    ch = FakeCH()
    assert bf.run(ch, limit=5, dry_run=False, cutoff=CUTOFF) == 0


def test_totally_failed_run_is_not_reported_as_success():
    """Очередь есть, а досчитать не удалось ничего — ненулевой код.

    Молчащий успех на полностью провалившемся прогоне — худший вид
    отчёта: работа не сделана, а выглядит сделанной.
    """
    ch = _ch(pending=[{"match_id": 7}], PlayerMatchFeatures=[])
    assert bf.run(ch, limit=5, dry_run=False, cutoff=CUTOFF) == 1
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
    assert bf.farm_rows(ch, 42) == []
    assert not any("PositionSnapshots" in q for q in ch.queries)


def test_match_without_last_hits_does_not_read_positions():
    """То же для матча без экономики: фарм не определить ничем."""
    ch = _ch(EconomyTimeline=[])
    ch.queries.clear()
    assert bf.farm_rows(ch, 42) == []
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
        bf.farm_rows(_ch(PlayerMatchFeatures=[]), 1)
        no_roster = caplog.text
        caplog.clear()
        bf.farm_rows(_ch(EconomyTimeline=[]), 2)
        no_economy = caplog.text
    assert "ростер" in no_roster
    assert "ростер" not in no_economy
    assert "экономик" in no_economy


# -- гашение старых клеток ------------------------------------------------------

def test_vanished_cells_are_zeroed_not_left_behind():
    """Клетка, которой в новом расчёте НЕТ, гасится нулём.

    Ради этого пересчёт и затевался. Новое определение убирает целые
    области — прежде всего фонтан. Вставка замещает клетку по ключу и про
    ключи, которых в ней нет, не знает: без гашения старое пятно на
    фонтане осталось бы на карте навсегда, и пересчёт выглядел бы
    выполненным.
    """
    ch = _ch()
    # Клетка на фонтане, посчитанная старым определением. Такой позиции в
    # POSITIONS нет, значит в новом расчёте её не будет.
    ch.existing = [{"phase": "early", "team": 2, "kind": "farm",
                    "gx": 0, "gy": 0}]
    rows = bf.farm_rows(ch, 42)
    zeros = [r for r in rows if r["n"] == 0]
    assert len(zeros) == 1
    assert (zeros[0]["gx"], zeros[0]["gy"]) == (0, 0)
    assert zeros[0]["kind"] == "farm"


def test_surviving_cells_are_not_zeroed():
    """Клетка, которая есть и в новом расчёте, гасится НЕ должна.

    Иначе пересчёт стирал бы ровно то, что подтвердил: строки с одним
    ключом и разным n схлопнутся по версии, и победить мог бы ноль.
    """
    ch = _ch()
    fresh = bf.farm_rows(ch, 42)
    live = [(r["phase"], r["team"], r["kind"], r["gx"], r["gy"])
            for r in fresh if r["n"] > 0]
    ch2 = _ch()
    ch2.existing = [{"phase": p, "team": t, "kind": k, "gx": gx, "gy": gy}
                    for p, t, k, gx, gy in live]
    rows = bf.farm_rows(ch2, 42)
    assert [r for r in rows if r["n"] == 0] == []


def test_zero_rows_are_dropped_by_the_reader():
    """Гашение работает только потому, что читатель отбрасывает n <= 0.

    Связь неочевидная и живёт в другом сервисе: измени reportgen фильтр —
    и погашенные клетки вернутся на карту как пустые, но существующие.
    Проверяем контракт здесь, чтобы он не разошёлся молча.
    """
    sys.path.insert(0, str(ROOT.parents[1] / "apps/report-generator/src"))
    from reportgen.heatmaps import build_heatmaps
    section = build_heatmaps([
        {"phase": "early", "team": 2, "kind": "farm", "gx": 1, "gy": 1, "n": 0},
        {"phase": "early", "team": 2, "kind": "farm", "gx": 2, "gy": 2, "n": 5},
    ])
    cells = section["phases"]["early"]["farm"]["radiant"]
    assert cells == [[2, 2, 5]]
