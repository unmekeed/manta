"""Ранг доезжает до витрины и по реплейному пути (спринт 199).

ЧТО БЫЛО. `avg_rank` живёт в витрине с миграции 018 — колонку завели
именно затем, чтобы «фильтр работает» стало ЗАПРОСОМ, а не рассуждением
про winrate. Но заполнял её только JSON-путь. Реплейный оставлял ноль, а
он даёт 82 матча в сутки против 14 JSON-овых: у 85% данных ярлык уровня
не сверялся ни с чем.

ПОЧЕМУ ЭТО ВАЖНО. У реплейного источника `tier` — КОНСТАНТА КЛАССА, а не
свойство матча: salts и candidates всегда ставят Pub. Утверждение это
никем не проверялось, а `tier` делит выборку на обучение и про-эталон,
по которому гейт судит каждую новую версию. Ошибка в разметке испортила
бы не данные, а МЕРУ — и все метрики остались бы правдоподобными.

ЧТО СТЕРЕЖЁТСЯ ЗДЕСЬ. Что ранг берётся из уже оплаченного ответа (лишний
вызов был бы платой за то, что уже куплено), что он доезжает в конверте
и что ноль означает «не знаем», а не «низкий».
"""
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector.runner import build_envelope  # noqa: E402
from collector.sources import MatchRef  # noqa: E402
from collector.sources.opendota_timeline import avg_rank  # noqa: E402


def detail(ranks: list[int]) -> dict:
    """Ответ `/matches/{id}` с заданными рангами игроков."""
    return {"match_id": 42, "patch": 60,
            "players": [{"rank_tier": r} for r in ranks]}


def test_the_envelope_carries_the_rank():
    """ГЛАВНОЕ: ранг едет в конверте наравне с tier и patch.

    Без него значение остаётся в коллекторе, а витрину пишет
    feature-extractor двумя сервисами дальше. Потеря на любом звене
    выглядит одинаково — нулём, то есть «не знаем», и отличить её от
    честной неизвестности нельзя.
    """
    ref = MatchRef(match_id=42, replay_url="s3://x/42.dem", tier="Premium",
                   source_cursor="42", patch=60, avg_rank=81)
    env = build_envelope(ref, ref.replay_url, "opendota_public")
    assert env["payload"]["avg_rank"] == 81


def test_an_unknown_rank_travels_as_zero_not_as_a_low_one():
    """Неизвестный ранг — ноль, и он ЗНАЧИТ «не знаем».

    Нулевого ранга не бывает: шкала начинается с 11 (Herald 1). Подставь
    сюда что-нибудь «нейтральное» вроде 40 — и аудит объявил бы
    нарушением честный пробел, то есть отправил бы чинить работающее.
    """
    ref = MatchRef(match_id=42, replay_url="s3://x/42.dem", tier="Pub",
                   source_cursor="42")
    assert build_envelope(ref, ref.replay_url, "salts")["payload"]["avg_rank"] == 0


def test_the_rank_is_taken_from_the_already_paid_response():
    """Ранг считается из деталей, за которые уже заплачено.

    `/matches/{id}` запрашивается ради адреса реплея и патча, и ранги
    игроков приходят тем же ответом. Отдельный вызов ради ранга стоил бы
    вторую оплату того же — при том что осмотр и так самый дорогой вызов
    цикла.
    """
    assert avg_rank(detail([80, 81, 82, 79, 80, 80, 81, 80, 80, 80])) >= 80


def test_too_few_known_ranks_give_zero_not_an_average_of_two():
    """Среднее по двум известным рангам из десяти — не среднее матча.

    Часть игроков скрывает профиль. Посчитать по оставшимся значило бы
    выдать оценку смещённой выборки за оценку матча — и она была бы
    правдоподобной, то есть незаметной.
    """
    assert avg_rank(detail([80, 80] + [0] * 8)) == 0


# -- источники, которые ранг знают ---------------------------------------------

def test_the_sources_that_paid_for_details_fill_the_rank():
    """`opendota_public` и `candidates` обязаны заполнять ранг.

    Оба держат в руках `detail` — ответ, из которого уже взяты адрес
    реплея и патч. Не заполнить ранг здесь значит выбросить данные, за
    которые заплачено, и оставить ярлык tier непроверяемым.

    Проверка текстовая, потому что построить эти источники без сети
    нельзя. Ищется присвоение, а не упоминание.
    """
    for name in ("opendota_public", "candidates"):
        src = (SRC / "collector" / "sources" / f"{name}.py").read_text(
            encoding="utf-8")
        assert "avg_rank=avg_rank(" in src, (
            f"{name}: ранг не кладётся в MatchRef, хотя detail на руках")


def test_the_sources_that_cannot_know_the_rank_do_not_invent_it():
    """Источники без деталей ранг НЕ выдумывают.

    `salts` берёт соль из своей таблицы, `opendota` собирает про-матчи, у
    которых ранг скрыт, `parked` возвращается к отложенным. Ни у одного
    нет ответа с рангами. Подставить туда что-нибудь — значит наполнить
    витрину числами, ничего не значащими, и сделать аудит бессмысленным.
    """
    for name in ("salts", "opendota", "parked"):
        src = (SRC / "collector" / "sources" / f"{name}.py").read_text(
            encoding="utf-8")
        assert "avg_rank=" not in src, (
            f"{name}: ранг проставляется источником, который его не знает")
