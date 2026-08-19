"""Поминутный слой тепловых карт в отчёте (спринт 148).

Вторая половина того, что просил владелец: «вместо размытых мид, лейт и
т.д. — ползунок времени и общая карта игры». Спринт 147 сделал данные,
здесь они доезжают до клиента.

ЧТО ЗДЕСЬ ВАЖНЕЕ ВСЕГО — нормировка. Ползунок показывает ОДНУ минуту, где
попаданий в клетку единицы; «вся игра» — сумму по всем минутам, где их
сотни. Одна шкала на оба режима сделала бы минутные кадры почти пустыми,
и это единственная ошибка в тепловой карте, которая не выглядит ошибкой:
пустая минута читается как «никто там не был».

Второе — что максимум за всю игру считается по СУММАМ, а не берётся из
поминутных максимумов. Герой, простоявший в одной клетке десять минут,
даёт там сумму втрое большую любого отдельного кадра; максимум из кадров
занизил бы шкалу, и общая карта вышла бы пересвеченной.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "libs"))

from reportgen.heatmaps import (build_heatmaps,  # noqa: E402
                                build_minute_heatmaps, heatmaps_available)


def _cell(minute, team, kind, gx, gy, n):
    return {"minute": minute, "team": team, "kind": kind,
            "gx": gx, "gy": gy, "n": n}


def test_empty_input_reports_no_minutes():
    """Матч до спринта 147: минутной разбивки нет и взять её неоткуда.

    Ноль минут — не пустая карта, а «этот слой не считался». Клиент по
    нему и решает, показывать ли ползунок вообще.
    """
    assert build_minute_heatmaps([]) == {"minutes": 0, "kinds": {}}
    assert build_minute_heatmaps(None) == {"minutes": 0, "kinds": {}}


def test_minute_lives_inside_the_cell():
    """Форма: [минута, gx, gy, n], а не ещё один уровень словаря."""
    got = build_minute_heatmaps([_cell(3, 2, "presence", 4, 5, 7)])
    assert got["minutes"] == 4
    assert got["kinds"]["presence"]["radiant"] == [[3, 4, 5, 7]]
    assert got["kinds"]["presence"]["dire"] == []


def test_sides_are_not_mixed():
    """Зеркальная по сторонам карта неотличима от настоящей на глаз."""
    got = build_minute_heatmaps([_cell(0, 2, "death", 1, 1, 1),
                                 _cell(0, 3, "death", 30, 30, 2)])
    block = got["kinds"]["death"]
    assert block["radiant"] == [[0, 1, 1, 1]]
    assert block["dire"] == [[0, 30, 30, 2]]


def test_per_minute_maximum_is_per_minute():
    """Шкала кадра — максимум ВНУТРИ его минуты.

    Возьми сюда максимум по всей игре, и минута с пятью попаданиями рядом
    с минутой на сто выглядела бы пустой.
    """
    got = build_minute_heatmaps([
        _cell(0, 2, "presence", 1, 1, 5),
        _cell(1, 2, "presence", 2, 2, 100),
    ])
    assert got["kinds"]["presence"]["max_by_minute"] == [5, 100]


def test_whole_game_maximum_is_the_max_of_sums():
    """Максимум за игру — по суммам клетки, а не по кадрам.

    Клетка (1,1) набирает 3+3+3=9 за три минуты, тогда как ни в одном
    кадре больше четырёх не встречается. Взять максимум из кадров значило
    бы нормировать общую карту на 4 и пересветить её.
    """
    cells = [_cell(m, 2, "presence", 1, 1, 3) for m in range(3)]
    cells.append(_cell(0, 2, "presence", 9, 9, 4))
    block = build_minute_heatmaps(cells)["kinds"]["presence"]
    assert block["max_n"] == 9
    assert max(block["max_by_minute"]) == 4


def test_max_by_minute_covers_every_minute_including_empty_ones():
    """Длина массива равна числу минут матча, а не числу непустых.

    Клиент индексирует его номером минуты напрямую; короткий массив дал
    бы сдвиг шкалы после первой же пустой минуты — и картинка осталась бы
    правдоподобной.
    """
    got = build_minute_heatmaps([_cell(0, 2, "ward", 1, 1, 1),
                                 _cell(4, 2, "ward", 2, 2, 3)])
    assert got["minutes"] == 5
    assert got["kinds"]["ward"]["max_by_minute"] == [1, 0, 0, 0, 3]


def test_broken_rows_are_dropped():
    """Испорченная строка не должна двигать шкалу.

    Клетка вне сетки и нулевое n приезжают из бэкфилла более старых
    версий; отрицательная минута невозможна в схеме (UInt16), но приходит
    и не из схемы — например, из теста или ручной вставки.
    """
    got = build_minute_heatmaps([
        _cell(0, 2, "presence", 99, 0, 5),      # вне сетки
        _cell(0, 2, "presence", 0, 0, 0),       # пустая
        _cell(-1, 2, "presence", 1, 1, 3),      # минуты не бывает
        _cell(0, 9, "presence", 1, 1, 3),       # нет такой стороны
        _cell(0, 2, "выдумка", 1, 1, 3),        # нет такого вида
        _cell(0, 2, "presence", 1, 1, 2),       # единственная годная
    ])
    assert got["kinds"]["presence"]["radiant"] == [[0, 1, 1, 2]]
    assert got["kinds"]["presence"]["max_n"] == 2
    assert list(got["kinds"]) == ["presence"]


def test_cells_are_ordered_deterministically():
    """Отчёт пересобирается при переразборе; плавающий порядок дал бы
    «изменившийся» отчёт там, где ничего не менялось."""
    cells = [_cell(2, 2, "farm", 3, 1, 1), _cell(0, 2, "farm", 5, 5, 1),
             _cell(2, 2, "farm", 1, 9, 1)]
    got = build_minute_heatmaps(cells)["kinds"]["farm"]["radiant"]
    assert got == [[0, 5, 5, 1], [2, 1, 9, 1], [2, 3, 1, 1]]


def test_minute_and_phase_layers_agree_on_totals():
    """Два слоя одного матча описывают одни и те же попадания.

    Расхождение здесь означало бы, что фазовая карта и поминутная считают
    разное — а увидеть это можно только сравнив числа: обе картинки по
    отдельности выглядят правдоподобно.
    """
    minute_rows = [_cell(m, team, "presence", m % 30, team, m + 1)
                   for m in range(30) for team in (2, 3)]
    # Фазовые строки строятся тем же правилом, что и в extractor.
    phase_rows = []
    for r in minute_rows:
        phase = ("early" if r["minute"] < 10
                 else "mid" if r["minute"] < 25 else "late")
        phase_rows.append({"phase": phase, "team": r["team"],
                           "kind": r["kind"], "gx": r["gx"], "gy": r["gy"],
                           "n": r["n"]})

    by_minute = build_minute_heatmaps(minute_rows)
    by_phase = build_heatmaps(phase_rows)

    minute_total = sum(c[3] for block in by_minute["kinds"].values()
                       for side in ("radiant", "dire") for c in block[side])
    phase_total = sum(c[2] for kinds in by_phase["phases"].values()
                      for block in kinds.values()
                      for side in ("radiant", "dire") for c in block[side])
    assert minute_total == phase_total


def test_availability_flag_still_reads_the_phase_layer():
    """`heatmaps_available` не должен реагировать на новый ключ.

    Флаг отвечает на вопрос «есть ли у матча карты вообще», а поминутного
    слоя у старых матчей нет по построению. Начни он смотреть и туда —
    все матчи до спринта 147 разом объявились бы без карт.
    """
    section = build_heatmaps([{"phase": "early", "team": 2, "kind": "ward",
                               "gx": 1, "gy": 1, "n": 1}])
    section["by_minute"] = {"minutes": 0, "kinds": {}}
    assert heatmaps_available(section) is True
