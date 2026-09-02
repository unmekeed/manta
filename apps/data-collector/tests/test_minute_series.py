"""Готовые поминутные ряды OpenDota (спринт 185).

ЗАЧЕМ ОНИ. Инвентаризация (спринт 184) показала: за один вызов
`/matches/{id}` приходит 149 полей на игрока, а читали мы четырнадцать.
Пять из невзятых — ГОТОВЫЕ ряды той же длины и той же сетки, что
`gold_t`: их не надо ни собирать из логов, ни восстанавливать по
событиям. Мы за них платили и выбрасывали.

Самый ценный — `hero_damage_t`. Модель знает только `networth_diff`, то
есть кто БОГАЧЕ; кто при этом ведёт бой, ей неизвестно. Команда,
лидирующая по золоту и отстающая по урону, — совсем другая игра, чем
просто богатая команда.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ. Ноль и пропуск. Ноль означает «никто не
фармил» — утверждение о матче; пропуск означает «данных нет». Спутать их
значит научить модель факту, которого не было.
"""
import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from collector import signals as S  # noqa: E402

# Индекс ряда — НОМЕР МИНУТЫ: gold_t[0] это нулевая минута, gold_t[1] —
# шестидесятая секунда. Поэтому сетка начинается с нуля, иначе первый
# запрошенный момент попал бы во второй элемент массива.
MIN = [0, 60, 120]


def match(radiant=None, dire=None, field="lh_t"):
    """Матч из двух игроков: слот 0 — Radiant, слот 128 — Dire."""
    players = []
    if radiant is not None:
        players.append({"player_slot": 0, field: radiant})
    if dire is not None:
        players.append({"player_slot": 128, field: dire})
    return {"players": players}


# -- считаем разность -----------------------------------------------------------

def test_the_series_becomes_a_team_difference():
    """Ряд превращается в разность Radiant − Dire.

    Все фичи витрины — командные дифференциалы; абсолютные значения
    сторон модель не видит и видеть не должна, иначе она выучит приор
    стороны вместо игры.
    """
    m = match(radiant=[10, 20, 30], dire=[4, 5, 6])
    out = S.minute_series_features(m, MIN)
    assert out["lh_diff"] == [10 - 4, 20 - 5, 30 - 6]


def test_all_five_series_are_taken():
    """Берутся все пять, а не какая-то одна.

    Список задан таблицей MINUTE_SERIES: добавить ряд — одна строка,
    и забыть его в одном из трёх мест (сигналы, витрина, фичи) нельзя.
    """
    assert set(S.MINUTE_SERIES) == {
        "lh_t", "dn_t", "hero_damage_t", "hero_healing_t", "camps_stacked_t"}
    m = {"players": [
        {"player_slot": 0, **{f: [1, 2, 3] for f in S.MINUTE_SERIES}},
        {"player_slot": 128, **{f: [1, 1, 1] for f in S.MINUTE_SERIES}}]}
    out = S.minute_series_features(m, MIN)
    assert set(out) == set(S.MINUTE_SERIES.values())


def test_the_grid_matches_the_requested_minutes():
    """Значение берётся по НОМЕРУ МИНУТЫ, как у gold_t.

    Сдвиг на минуту не падает и не виден в логе — он просто смещает
    признак относительно исхода, то есть отравляет обучение тихо.
    """
    m = match(radiant=[0, 100, 200, 300], dire=[0, 0, 0, 0])
    out = S.minute_series_features(m, [0, 60, 180])
    assert out["lh_diff"] == [0, 100, 300]


def test_beyond_the_end_the_last_value_holds():
    """За хвостом ряда держится последнее известное значение.

    Матч мог кончиться раньше последней минуты сетки. Ноль там означал
    бы «показатель обнулился» — то есть что все добитки вдруг исчезли.
    """
    m = match(radiant=[5, 9], dire=[0, 0])
    out = S.minute_series_features(m, [60, 120, 600])
    assert out["lh_diff"] == [9, 9, 9]


# -- ноль против пропуска -------------------------------------------------------

def test_a_missing_series_is_not_reported_as_zero():
    """ГЛАВНОЕ: ряда нет ни у кого — фичи нет вовсе.

    Матч разобран старой версией парсера OpenDota. Ноль здесь был бы
    УТВЕРЖДЕНИЕМ («никто не фармил»), а не пропуском; LightGBM
    обрабатывает пропуск нативно, а ноль он примет за факт и выучит его.
    """
    m = {"players": [{"player_slot": 0}, {"player_slot": 128}]}
    assert S.minute_series_features(m, MIN) == {}


def test_one_player_without_the_series_does_not_void_the_team():
    """А пропуск у ОДНОГО игрока не выбрасывает данные девяти остальных.

    Обратное решение стоило бы целой команды из-за одного покинувшего
    игру.
    """
    m = {"players": [{"player_slot": 0, "lh_t": [10, 20, 30]},
                     {"player_slot": 1},
                     {"player_slot": 128, "lh_t": [1, 2, 3]}]}
    out = S.minute_series_features(m, MIN)
    assert out["lh_diff"] == [9, 18, 27]


def test_garbage_in_the_series_does_not_crash_the_match():
    """Мусор в ряду не роняет разбор.

    Ответы API бывают битыми, и один null посреди массива не должен
    стоить всего матча.
    """
    m = match(radiant=[1, None, "x"], dire=[0, 0, 0])
    out = S.minute_series_features(m, MIN)
    assert out["lh_diff"][0] == 1
    assert out["lh_diff"][1] == 0


# -- проводка -------------------------------------------------------------------

def test_the_series_reach_the_aggregate():
    """Фичи доезжают до `all_minute_features`.

    Написанная, но не подключённая функция — код, который никогда не
    выполнится, и заметить это можно только руками.
    """
    m = {"players": [{"player_slot": 0, "lh_t": [7, 7, 7]},
                     {"player_slot": 128, "lh_t": [2, 2, 2]}]}
    assert S.all_minute_features(m, MIN).get("lh_diff") == [5, 5, 5]


@pytest.mark.parametrize("name", ["lh_diff", "dn_diff", "hero_damage_diff",
                                  "hero_healing_diff", "camps_stacked_diff"])
def test_every_new_column_is_written_to_the_mart(name):
    """Каждая новая фича попадает в запись витрины.

    Посчитать и не записать — самый тихий из возможных исходов: логи
    чисты, колонка пуста.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "collector"
           / "timeline_runner.py").read_text(encoding="utf-8")
    assert f'"{name}"' in src


def test_the_nan_tail_covers_the_new_columns():
    """Хвост nan-заполнения расширен вместе со списком колонок.

    В timeline_runner срез F_TRACK_COLUMNS считается ОТ КОНЦА, и
    добавленная колонка не попадёт в заполнение nan-ами, если число не
    поправить. Тогда у матча с битым JSON она молча станет нулём —
    ровно та подмена пропуска фактом, от которой защищает весь этот файл.
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "collector"
           / "timeline_runner.py").read_text(encoding="utf-8")
    # Разбор через ast, а не нарезкой по запятым: в комментариях внутри
    # списка запятые есть, и наивный парсер терял колонку, стоящую сразу
    # за комментарием. Поймано на первом прогоне — он «не нашёл» и
    # roshan_diff, существующий с трека F.
    names = []
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "MTF_COLUMNS"):
            names = [e.value for e in node.value.elts]
    assert names, "MTF_COLUMNS не разобрался"
    n_f_track = int(src.split("F_TRACK_COLUMNS = MTF_COLUMNS[-", 1)[1]
                    .split(":", 1)[0])
    tail = names[-n_f_track:]
    # Проверять «новые колонки в хвосте» НЕДОСТАТОЧНО: при укороченном
    # срезе они всё равно попадают в конец, а выпадают СТАРЫЕ фичи трека
    # F. Поймано мутацией (-19 → -14 прошло незамеченным). Поэтому хвост
    # сверяется целиком: он обязан начинаться ровно с первой фичи трека,
    # то есть покрывать всё, чего реплейный путь не считает.
    assert tail[0] == "roshan_diff", (
        f"хвост nan-заполнения начинается с {tail[0]!r}, а должен — с "
        "первой фичи трека F: колонки перед ней потеряют пропуски")
    for name in S.MINUTE_SERIES.values():
        assert name in tail, f"{name} вне хвоста nan-заполнения"
