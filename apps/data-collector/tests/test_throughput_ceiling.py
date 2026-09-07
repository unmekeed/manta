"""Потолок притока назван и согласован (спринт 196).

ЖИВОЙ ЗАМЕР 07.09.2026. Датасет рос на ~72 матча в сутки при потраченных
2% месячного бюджета. Упирался сбор не в деньги, а в три независимых
числа, ни одно из которых не отвечало на вопрос «сколько матчей в сутки
нам нужно»:

  * `OPENDOTA_GLOBAL_LIMIT` — 1900, потолок БЕСПЛАТНОГО тарифа, оставшийся
    после перехода на платный ключ;
  * `SHARES` — абсолютные доли, сумма которых ровно 1900, то есть
    привязанные к тому же устаревшему числу;
  * `TIMELINE_LIMIT` — 10 матчей за цикл при цикле в 1800 секунд, то есть
    потолок 480 матчей в сутки САМ ПО СЕБЕ.

Связывало их только совпадение. Настоящим тормозом был третий, и об этом
не говорила ни одна настройка: чтобы его увидеть, надо было перемножить
число из compose на интервал из другой строки compose и сравнить с долей
из исходника.

ЧТО ЭТОТ ФАЙЛ СТЕРЕЖЁТ. Что цель по притоку объявлена числом и что
конфигурация до неё дотягивает. Тормоз должен быть ОДИН и назван —
объявленный месячный потолок, он же деньги. Любое число, ограничивающее
приток раньше него, обязано ронять этот тест.
"""
import pathlib

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора compose")

import sys  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector import budget  # noqa: E402

COMPOSE = ROOT / "deployments" / "docker-compose.yml"

# Сервис compose → имя источника в SHARES. Только JSON-путь: реплейные
# источники ограничены диском, а не квотой, и в цель по притоку витрины
# они входят иначе (один матч — один реплей на 58 МиБ).
JSON_SERVICES = {
    "timeline-collector": "opendota-timeline",
    "pro-timeline-collector": "opendota-timeline-pro",
    "league-collector": "opendota-league",
    "stratz-collector": "stratz-timeline",
}


def compose() -> dict:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor(
        "!override",
        lambda ldr, node: ldr.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode) else ldr.construct_object(node))
    return yaml.load(COMPOSE.read_text(encoding="utf-8"), Loader=Loader)


def default_of(raw: str) -> int:
    """Умолчание из записи вида `${VAR:-30}`.

    Берётся именно умолчание: оно и работает на машине, где переменную не
    задали, то есть описывает поведение системы «из коробки».
    """
    assert ":-" in raw, f"нет умолчания в {raw!r}"
    return int(raw.split(":-", 1)[1].rstrip('}"'))


def knobs(service: str) -> tuple[int, int]:
    """(матчей за цикл, секунд между циклами) для сервиса."""
    env = compose()["services"][service]["environment"]
    return (default_of(str(env["TIMELINE_LIMIT"])),
            default_of(str(env["COLLECTOR_INTERVAL_SECONDS"])))


def test_the_parsing_finds_real_numbers():
    """Страховка от проверки пустоты.

    Сломайся разбор compose — сравнения ниже прошли бы на нулях либо на
    пустом наборе сервисов.
    """
    for service in JSON_SERVICES:
        limit, interval = knobs(service)
        assert limit > 0 and interval > 0, (service, limit, interval)


def test_the_target_is_derived_from_the_declared_money():
    """Цель посчитана из объявленного месячного потолка, а не выбрана.

    500 000 вызовов в месяц при двух вызовах на матч — это около 8000
    матчей в сутки. Цель, взятая с потолка, не выдержала бы вопроса
    «почему столько», и первый же спор о ней кончился бы её тихой
    правкой.
    """
    monthly = budget.TARGET_MATCHES_PER_DAY * budget.CALLS_PER_MATCH * 30
    assert 400_000 <= monthly <= 600_000, (
        f"цель {budget.TARGET_MATCHES_PER_DAY}/сут требует {monthly} вызовов "
        f"в месяц — это больше не тот бюджет, из которого она посчитана")


@pytest.mark.parametrize("service", sorted(JSON_SERVICES))
def test_the_per_cycle_limit_is_not_the_hidden_brake(service):
    """ГЛАВНОЕ: лимит за цикл не должен упираться раньше бюджета.

    Ровно этот дефект и нашли: `TIMELINE_LIMIT=10` при цикле 1800 секунд
    держал приток на 480 матчах в сутки независимо от того, сколько денег
    разрешено потратить. Тормоз был, но НЕ БЫЛ НАЗВАН — ни в одной
    настройке не написано «потолок 480».

    Считаем так: при потолке квоты, достаточном для цели, доля источника
    позволяет ему собрать столько-то матчей в сутки. Столько же он обязан
    успевать и по своим циклам.
    """
    source = JSON_SERVICES[service]
    share = budget.shares_for(budget.ceiling_for_target())[source]

    limit, interval = knobs(service)
    # Считаем в ОСМОТРАХ, а не в матчах, потому что упирается именно в них.
    # Ранг известен только после `/matches/{id}`, поэтому вызов тратится и
    # на кандидата, которого мы отвергнем; за цикл источник осматривает
    # `detail_budget`, а он по умолчанию вдвое больше лимита матчей
    # (`opendota_timeline.py`: `detail_budget or 2 * limit_per_cycle`).
    #
    # Сравнение в матчах прошло бы почти при любых числах и потому ничего
    # не стерегло бы: лимит матчей за цикл почти всегда больше, чем матчей
    # получается. Первая редакция этого теста именно так и была написана.
    inspections = 2 * limit * (86400 / interval)

    assert inspections >= share, (
        f"{service}: доля позволяет {share} осмотров в сутки, а циклы дают "
        f"{inspections:.0f} ({limit} матчей за цикл → {2 * limit} осмотров, "
        f"раз в {interval}с). Приток упрётся в число из compose, а не в "
        f"объявленный бюджет")


def test_the_free_tier_shares_are_unchanged():
    """Подъём потолка не переставил то, что уже работало.

    При 1900 (бесплатный тариф) доли обязаны совпадать с прежними
    абсолютными числами. Иначе машина без ключа получила бы другую
    раскладку от изменения, которое её не касается.
    """
    assert budget.shares_for(1900) == {
        "candidates": 1100, "opendota-public": 50, "opendota": 50,
        "opendota-timeline": 250, "opendota-timeline-pro": 200,
        "opendota-league": 200, "stratz-timeline": 50}


@pytest.mark.parametrize("limit", [1900, 5000, 15000, 17200, 40000])
def test_the_guarantees_cover_the_whole_ceiling(limit):
    """ГЛАВНОЕ: сумма долей равна потолку — иначе гарантии беззубы.

    ЖИВОЙ СЛУЧАЙ 07.09.2026. На VPS потолок был поднят руками до 15000, а
    `SHARES` остались абсолютными числами с суммой 1900. Резерв под чужие
    гарантии считается ИМЕННО ОТ НИХ, поэтому утром источник получал
    потолок 13350 из 15000 вместо своей доли: гарантии превратились
    обратно в «кто успел» — ровно в то, ради предотвращения чего модуль
    бюджета и написан (простой 2026-08-06, шесть часов).

    Дефект был невидим с обеих сторон. Смотришь на потолок — он поднят и
    щедр. Смотришь на доли — они те же, что и работали. Разъехались не
    числа, а СВЯЗЬ между ними, и её никто не проверял.

    Допуск в число источников — это потери на целочисленном делении,
    по одному вызову на источник, не больше.
    """
    shares = budget.shares_for(limit)
    total = sum(shares.values())
    assert limit - len(shares) <= total <= limit, (
        f"доли дают {total} при потолке {limit}: резерв под чужие гарантии "
        f"считается от суммы долей, и разрыв в {limit - total} вызовов "
        f"означает, что настолько гарантии не работают")


def test_the_replay_shares_do_not_grow_with_the_ceiling():
    """Реплейные доли не растут вместе с квотой — их держит диск.

    Каждый матч это ~58 МиБ .dem. Вырасти доля кандидатов вместе с
    потолком в шестнадцать тысяч вызовов — и это попытка скачать порядка
    десяти тысяч реплеев в сутки, около 580 ГиБ. Отказ был бы не тихим,
    но и не быстрым: диск кончился бы посреди ночи.
    """
    small = budget.shares_for(1900)
    large = budget.shares_for(budget.ceiling_for_target())
    for name in budget.REPLAY_SHARES:
        assert small[name] == large[name], (
            f"{name}: доля выросла с потолком, хотя ограничена диском")
    assert large["opendota-timeline"] > small["opendota-timeline"], (
        "JSON-доля не выросла — подъём потолка ни на что не повлиял")


# -- откуда берётся суточный потолок --------------------------------------------

def test_without_a_key_the_ceiling_stays_at_the_free_tier(monkeypatch):
    """Нет ключа — тариф бесплатный, и 1900 не наш выбор, а их лимит."""
    monkeypatch.delenv("OPENDOTA_GLOBAL_LIMIT", raising=False)
    monkeypatch.delenv("OPENDOTA_API_KEY", raising=False)
    assert budget.daily_limit() == budget.FREE_TIER_LIMIT


def test_a_key_without_a_declared_monthly_ceiling_does_not_open_the_tap(
        monkeypatch):
    """Ключ есть, месячный потолок не объявлен — суточный не поднимаем.

    Снять тормоз, не узнав, сколько разрешено тратить, значит выписать
    себе счёт вслепую: у платного тарифа суточной квоты нет, есть деньги.
    """
    monkeypatch.delenv("OPENDOTA_GLOBAL_LIMIT", raising=False)
    monkeypatch.setenv("OPENDOTA_API_KEY", "ключ")
    monkeypatch.setattr(budget, "MONTHLY_LIMIT", 0)
    assert budget.daily_limit() == budget.FREE_TIER_LIMIT


def test_the_ceiling_comes_from_the_declared_monthly_budget(monkeypatch):
    """ГЛАВНОЕ: суточный потолок — это месяц, делённый на длину месяца.

    Так суточный расход по построению не выходит за деньги, которые
    разрешили потратить, и поднимается бюджет в ОДНОМ месте — там, где он
    про деньги и сказан.
    """
    monkeypatch.delenv("OPENDOTA_GLOBAL_LIMIT", raising=False)
    monkeypatch.setenv("OPENDOTA_API_KEY", "ключ")
    monkeypatch.setattr(budget, "MONTHLY_LIMIT", 500_000)
    from datetime import datetime, timezone

    sept = datetime(2026, 9, 15, tzinfo=timezone.utc)   # 30 дней
    assert budget.daily_limit(sept) == 500_000 // 30


def test_an_explicit_ceiling_wins_over_the_formula(monkeypatch):
    """Явно заданное владельцем число сильнее любой формулы.

    Иначе аварийное «прижать сбор до 500 вызовов» молча игнорировалось бы
    ради посчитанного значения — в тот момент, когда прижать и нужно.
    """
    monkeypatch.setenv("OPENDOTA_GLOBAL_LIMIT", "500")
    monkeypatch.setenv("OPENDOTA_API_KEY", "ключ")
    monkeypatch.setattr(budget, "MONTHLY_LIMIT", 500_000)
    assert budget.daily_limit() == 500


def test_a_tight_ceiling_starves_everyone_a_little_not_json_entirely(
        monkeypatch):
    """Потолок ниже реплейных долей не обнуляет JSON-путь.

    Ноль здесь означал бы остановку основного притока витрины из-за
    опечатки в переменной окружения — и выглядело бы это как «сбор
    работает, просто матчей нет».
    """
    tight = budget.shares_for(600)
    assert all(v >= 1 for v in tight.values()), tight
    assert tight["opendota-timeline"] >= 1
