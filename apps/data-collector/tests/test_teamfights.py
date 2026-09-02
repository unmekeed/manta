"""Драки как события с исходом (спринт 186).

ЗАЧЕМ ОТДЕЛЬНО ОТ УБИЙСТВ И УРОНА. У модели уже есть `kills_diff` (все
убийства матча) и `hero_damage_diff` (весь урон, спринт 185). Драка — не
сумма, а СОБЫТИЕ: пять смертей, размазанные по карте за десять минут, и
пять смертей в одном замесе — разные игры. Первое значит, что кого-то
ловят поодиночке; второе — что команда проиграла бой и следующие полминуты
у неё нет ни одного героя на карте.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ. Незавершённая драка. Данные по игрокам API
отдаёт за ВСЮ драку целиком, поэтому учесть идущий бой частично нельзя, а
учесть целиком — значит показать модели исход, которого на этой минуте
ещё не случилось. Это утечка будущего в признаки: на обучении метрика
улучшится, в проде модель ослепнет ровно в момент драки.
"""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector import signals as S  # noqa: E402

SLOTS = [0, 1, 2, 3, 4, 128, 129, 130, 131, 132]


def fight(end, deaths_r=(0,) * 5, deaths_d=(0,) * 5, gold_r=(0,) * 5,
          gold_d=(0,) * 5, start=None):
    players = [{"deaths": d, "gold_delta": g}
               for d, g in zip(deaths_r, gold_r)]
    players += [{"deaths": d, "gold_delta": g}
                for d, g in zip(deaths_d, gold_d)]
    return {"start": start if start is not None else end - 30,
            "end": end, "deaths": sum(deaths_r) + sum(deaths_d),
            "players": players}


def match(*fights):
    return {"players": [{"player_slot": s} for s in SLOTS],
            "teamfights": list(fights)}


# -- исход драки ----------------------------------------------------------------

def test_the_side_that_lost_fewer_heroes_wins_the_fight():
    """Выиграла сторона с МЕНЬШИМИ потерями.

    Это и есть определение выигранной драки: не «кто больше убил» (в
    сумме это те же числа), а кто остался на карте.
    """
    m = match(fight(end=300, deaths_r=(1, 0, 0, 0, 0), deaths_d=(1, 1, 1, 0, 0)))
    out = S.teamfight_features(m, [300])
    assert out["fights_won_diff"] == [1.0]


def test_an_even_trade_is_nobody_s_win():
    """Равный размен не засчитывается никому.

    Три на три — это размен, а не победа, и записывать его в счёт значило
    бы называть победой любое столкновение.
    """
    m = match(fight(end=300, deaths_r=(1, 1, 0, 0, 0), deaths_d=(1, 1, 0, 0, 0)))
    assert S.teamfight_features(m, [300])["fights_won_diff"] == [0.0]


def test_fights_accumulate_over_the_match():
    """Счёт накопительный: две выигранные драки дают два.

    Модель смотрит на состояние, а не на последнее событие: одна
    выигранная драка на десятой минуте и пять на тридцатой — разные
    матчи.
    """
    m = match(fight(end=300, deaths_d=(1, 0, 0, 0, 0)),
              fight(end=600, deaths_d=(1, 0, 0, 0, 0)),
              fight(end=900, deaths_r=(1, 0, 0, 0, 0)))
    out = S.teamfight_features(m, [300, 600, 900])
    assert out["fights_won_diff"] == [1.0, 2.0, 1.0]


# -- утечка будущего ------------------------------------------------------------

def test_an_unfinished_fight_is_not_counted():
    """ГЛАВНОЕ: идущая драка в признаки не попадает.

    API отдаёт данные по игрокам за ВСЮ драку, поэтому учесть её на
    середине нельзя, а учесть целиком — значит показать модели исход,
    которого на этой минуте ещё не было. На обучении такая утечка
    улучшает метрику, а в проде модель слепнет ровно в момент боя.
    """
    m = match(fight(start=280, end=400, deaths_d=(1, 1, 0, 0, 0)))
    out = S.teamfight_features(m, [300, 360, 420])
    assert out["fights_won_diff"] == [0.0, 0.0, 1.0]


def test_time_since_the_fight_counts_from_its_end():
    """Время считается от КОНЦА драки, а не от начала.

    От начала оно росло бы во время самого боя — то есть говорило бы
    «давно ничего не происходило» в разгар замеса.
    """
    m = match(fight(start=200, end=300))
    out = S.teamfight_features(m, [360, 600])
    assert out["since_fight_s"] == [60.0, 300.0]


def test_before_the_first_fight_the_time_is_unknown():
    """До первой драки времени «с прошлой» не существует.

    Ноль означал бы «драка только что кончилась» — самое неверное из
    возможных. Пропуск LightGBM обрабатывает нативно, а game_time у
    модели и так есть.
    """
    m = match(fight(end=600))
    out = S.teamfight_features(m, [300, 600])
    assert math.isnan(out["since_fight_s"][0])
    assert out["since_fight_s"][1] == 0.0


# -- ноль против пропуска -------------------------------------------------------

def test_a_match_without_teamfights_gives_no_features():
    """Драк нет в данных — фич нет вовсе.

    «Драк не случилось» и «матч разобран старой версией парсера»
    выглядят одинаково, а нули были бы утверждением о матче.
    """
    m = {"players": [{"player_slot": s} for s in SLOTS], "teamfights": []}
    assert S.teamfight_features(m, [300]) == {}


def test_a_fight_without_an_end_is_skipped_not_guessed():
    """Драка без времени конца пропускается, а не достраивается.

    Придумать ей конец значило бы придумать момент, с которого модель
    считает исход известным.
    """
    # Сломанная драка АСИММЕТРИЧНА: будь она равной, её учёт ничего бы не
    # изменил и мутация «достроить end нулём» прошла бы незамеченной.
    # Поймано мутацией.
    broken = {"start": 100,
              "players": [{"deaths": 1, "gold_delta": 500} for _ in SLOTS[:5]]
                         + [{"deaths": 0, "gold_delta": 0} for _ in SLOTS[5:]]}
    m = match(fight(end=300, deaths_d=(1, 0, 0, 0, 0)))
    m["teamfights"].append(broken)
    out = S.teamfight_features(m, [300])
    assert out["fights_won_diff"] == [1.0], "сломанная драка попала в счёт"
    assert out["fight_gold_diff"] == [0.0], "золото сломанной драки учтено"


# -- знаки ----------------------------------------------------------------------

def test_gold_from_fights_is_a_radiant_minus_dire_difference():
    """Золото драк — разность R−D, как все прочие фичи витрины."""
    m = match(fight(end=300, gold_r=(100, 100, 0, 0, 0), gold_d=(50, 0, 0, 0, 0)))
    assert S.teamfight_features(m, [300])["fight_gold_diff"] == [150.0]


def test_deaths_keep_the_literal_sign():
    """Смерти — БУКВАЛЬНО Radiant − Dire, без «удобного» разворота.

    Плюс здесь значит «Radiant теряет больше», то есть плохо для
    Radiant. Развернуть знак ради удобства значило бы завести исключение
    из правила «все diff — это R−D», а исключение стоит дороже
    неудобства: следующий читатель применит правило и ошибётся.
    """
    m = match(fight(end=300, deaths_r=(1, 1, 0, 0, 0), deaths_d=(1, 0, 0, 0, 0)))
    assert S.teamfight_features(m, [300])["fight_deaths_diff"] == [1.0]


def test_sides_come_from_player_slots_not_from_order():
    """Сторона берётся из player_slot, а не из «первые пять — Radiant».

    Порядок задан данными, а не соглашением; полагаться на него значит
    молча перепутать команды, если он изменится.
    """
    m = match(fight(end=300, deaths_d=(1, 1, 0, 0, 0)))
    m["players"] = [{"player_slot": s} for s in
                    [128, 129, 130, 131, 132, 0, 1, 2, 3, 4]]
    # Теперь первые пять — Dire, значит потери у них, и выигрывает Radiant
    # (со знаком −1, потому что стороны в списке переставлены).
    assert S.teamfight_features(m, [300])["fights_won_diff"] == [-1.0]


def test_the_features_reach_the_aggregate():
    """Фичи доезжают до `all_minute_features`.

    Написанная и не подключённая функция — код, который никогда не
    выполнится.
    """
    m = match(fight(end=300, deaths_d=(1, 0, 0, 0, 0)))
    assert S.all_minute_features(m, [300]).get("fights_won_diff") == [1.0]
