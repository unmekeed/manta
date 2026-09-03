"""Карточка матча для списка `/api/v1/matches` (спринт 192).

Карточка — КОНТРАКТ с сайтом, и ошибиться в ней дорого именно потому,
что ошибка правдоподобна: перепутанные стороны дают список, который
выглядит совершенно нормально, а показывает составы наоборот.

Две вещи, которые здесь проверяются жёстче прочего:

  * КОДЫ СТОРОН. В `PlayerMatchFeatures` команда — это 2 (Radiant) и 3
    (Dire), коды Valve, а не 0/1. Ноль там означает «поле не заполнено»,
    и трактовать его как Radiant значило бы сваливать битые строки в
    одну из команд.
  * НОЛЬ ПРОТИВ ПРОПУСКА. Финальная WP у матча без обслуживаемой модели
    ОТСУТСТВУЕТ. Подставить 0.5 значит показать пользователю выдуманное
    число, неотличимое от честной равной позиции.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from reportgen.summary import (DIRE_TEAM, RADIANT_TEAM,  # noqa: E402
                               build_summary)

ROWS = [{"game_time": 60, "radiant_win": 1, "kills_radiant": 1,
         "kills_dire": 0, "patch": 57, "tier": "Premium"},
        {"game_time": 1800, "radiant_win": 1, "kills_radiant": 32,
         "kills_dire": 18, "patch": 57, "tier": "Premium"}]


def players(duration=2145):
    out = []
    for i in range(5):
        out.append({"player_id": i, "team": RADIANT_TEAM,
                    "hero": f"npc_dota_hero_r{i}", "duration_s": duration})
    for i in range(5):
        out.append({"player_id": 5 + i, "team": DIRE_TEAM,
                    "hero": f"npc_dota_hero_d{i}", "duration_s": duration})
    return out


ANALYSIS = {"win_probability": {"final_radiant": "0.87"}}


def test_the_card_takes_the_outcome_from_the_last_row():
    """Исход, счёт, патч и уровень берутся из ПОСЛЕДНЕЙ строки витрины.

    Первая строка — шестидесятая секунда, там счёт ещё 1:0. Взять её по
    ошибке (например, `rows[0]`) не уронит ничего: карточки просто будут
    показывать счёт первой минуты каждого матча.
    """
    card = build_summary(42, ROWS, players(), ANALYSIS)
    assert card["kills_radiant"] == 32 and card["kills_dire"] == 18
    assert card["radiant_win"] is True
    assert card["patch"] == 57 and card["tier"] == "Premium"


def test_sides_come_from_valve_codes_not_from_order():
    """Составы разводятся по коду команды, а не по порядку игроков.

    Разложить первых пятерых в Radiant «потому что player_id 0-4» —
    соблазнительно и почти всегда верно. Почти: строка с чужим порядком
    даст карточку, где составы поменяны местами, и заметить это можно
    только сверив с игрой.
    """
    ps = players()
    ps.reverse()
    card = build_summary(42, ROWS, ps, ANALYSIS)
    assert sorted(card["radiant_heroes"]) == [f"npc_dota_hero_r{i}"
                                              for i in range(5)]
    assert sorted(card["dire_heroes"]) == [f"npc_dota_hero_d{i}"
                                           for i in range(5)]


def test_an_unknown_team_code_does_not_land_in_radiant():
    """Нераспознанный код команды не приписывается Radiant.

    Ноль в поле `team` означает «не заполнено». Radiant — это ровно 2.
    """
    ps = players()
    ps[0]["team"] = 0
    card = build_summary(42, ROWS, ps, ANALYSIS)
    assert "npc_dota_hero_r0" not in card["radiant_heroes"]


def test_a_player_without_a_hero_is_skipped_not_written_as_empty():
    """Игрок без героя не даёт пустую строку в составе.

    Пустое имя доехало бы до сайта как герой без картинки и без имени —
    видимый мусор в карточке.
    """
    ps = players()
    ps[0]["hero"] = ""
    card = build_summary(42, ROWS, ps, ANALYSIS)
    assert "" not in card["radiant_heroes"]
    assert len(card["radiant_heroes"]) == 4


# -- длительность --------------------------------------------------------------

def test_duration_comes_from_the_mart_not_from_the_grid():
    """Длительность берётся из `duration_s`, а не из сетки таймлайна.

    Сетка поминутная, поэтому оценка по ней ВСЕГДА занижена и округлена
    вниз. Молчаливое использование её как основной укоротило бы каждый
    матч в списке.
    """
    card = build_summary(42, ROWS, players(duration=2145), ANALYSIS)
    assert card["duration_s"] == 2145


def test_without_duration_the_last_timeline_point_is_used():
    """Запасной вариант хуже, но лучше нуля: матч длился не ноль секунд."""
    card = build_summary(42, ROWS, players(duration=0), ANALYSIS)
    assert card["duration_s"] == 1800


# -- ноль против пропуска ------------------------------------------------------

def test_final_wp_is_none_when_the_model_did_not_score():
    """ГЛАВНОЕ: неизвестная WP — это None, а не 0.5.

    Половина означает «позиция ровная» — утверждение о матче. Отсутствие
    модели означает «мы не считали». Сайт обязан уметь показать второе
    иначе, чем первое, а для этого различие должно доехать до него.
    """
    assert build_summary(42, ROWS, players(), {})["final_radiant_wp"] is None
    assert build_summary(42, ROWS, players(),
                         {"win_probability": {}})["final_radiant_wp"] is None
    assert build_summary(42, ROWS, players(),
                         {"win_probability": {"final_radiant": ""}}
                         )["final_radiant_wp"] is None


def test_final_wp_zero_is_kept_as_zero():
    """А вот ноль от модели — это ноль, и терять его нельзя.

    Проверка вида `if raw:` превратила бы честный «Radiant проиграл
    наверняка» в «не считали». Ловится только отдельным случаем.
    """
    card = build_summary(42, ROWS, players(),
                         {"win_probability": {"final_radiant": "0.0"}})
    assert card["final_radiant_wp"] == 0.0


def test_final_wp_is_read_as_a_number():
    card = build_summary(42, ROWS, players(), ANALYSIS)
    assert card["final_radiant_wp"] == 0.87


# -- проводка ------------------------------------------------------------------

def test_every_column_of_the_table_is_filled():
    """Ключи карточки совпадают с колонками, которые пишет UPSERT.

    Разъезд здесь не падает при разработке — psycopg сообщит о
    недостающем параметре только на живой базе, то есть в production.
    """
    from reportgen.summary import UPSERT_SQL

    card = build_summary(42, ROWS, players(), ANALYSIS)
    named = {p.split(")")[0] for p in UPSERT_SQL.split("%(")[1:]}
    assert named == set(card), (
        f"UPSERT ждёт {sorted(named)}, карточка даёт {sorted(card)}")
