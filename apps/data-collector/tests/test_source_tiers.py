"""Уровень матча восстановим по имени источника (спринт 195a).

ЖИВОЙ ЗАМЕР 6 сентября 2026. Первый боевой прогон `republish_orphans.py`
нашёл 510 матчей, чей реплей лежит в S3, а разбора нет, — и переотправить
собрался 132. Остальные 361 пропускались с формулировкой «уровень матча
неизвестен».

Причина оказалась круговой. Уровень инструмент брал из витрины, а витрину
реплейного матча пишет feature-extractor — тот самый шаг, который у сироты
и не состоялся. То есть уровень отсутствовал ровно у тех матчей, ради
которых инструмент и написан, а восстанавливались лишь те, что до реплея
успели приехать JSON-путём. Осторожность была верной, источник — нет.

ЧЕСТНЫЙ ВТОРОЙ ИСТОЧНИК. У реплейного источника уровень не вычисляется
по матчу, а является его СВОЙСТВОМ: salts всегда даёт Pub, opendota —
Professional. Значит имя источника, которое `CollectedMatches` хранит,
определяет уровень однозначно. Это не догадка: значение берётся из того
же места, что и при первом сборе.

ЧЕГО ЗДЕСЬ СТЕРЕГУТ. Ровно того, что делает подстановку догадкой:
разъезда между тем, что источник объявляет, и тем, что он кладёт в
MatchRef. Пока это одно значение — восстановление точное. Появись второй
литерал рядом с константой, и тест перестанет быть про уровень: он станет
про надежду.
"""
import pathlib
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector.sources import MatchRef, replay_source_tiers  # noqa: E402
from collector.sources.fixture import FixtureSource  # noqa: E402

SOURCES_DIR = SRC / "collector" / "sources"

# Уровни из спецификации (Гл. 4.2). Список закрытый: значение вне его
# означает опечатку, а опечатка в tier тиха — матч просто не попадёт ни в
# обучение, ни в эталон.
KNOWN_TIERS = {"Pub", "Premium", "Professional", "Tournament"}


def replay_source_files() -> list[pathlib.Path]:
    """Файлы источников реплейного пути.

    Признак — метод `download_replay`: это ровно то, что отличает
    источник, дающий реплей, от JSON-источника. Перечислять их руками
    значило бы завести третий список имён — как раз то, что разъехалось в
    спринте 180.
    """
    return [p for p in sorted(SOURCES_DIR.glob("*.py"))
            if p.name != "__init__.py"
            and "def download_replay" in p.read_text(encoding="utf-8")]


def test_the_discovery_finds_the_sources_at_all():
    """Страховка от проверки пустоты.

    Сломайся признак — все проверки ниже стали бы тождественно зелёными,
    перебирая пустой список.
    """
    found = replay_source_files()
    assert len(found) >= 5, [p.name for p in found]


@pytest.mark.parametrize("path", replay_source_files(),
                         ids=lambda p: p.name)
def test_every_replay_source_declares_its_tier(path):
    """У каждого реплейного источника уровень объявлен константой.

    Без объявления имя источника ничего не значит, и матчи этого
    источника молча уходят в пропуск при восстановлении — то есть
    остаются потерянными.
    """
    src = path.read_text(encoding="utf-8")
    found = re.search(r'^\s*TIER = "([^"]+)"', src, re.M)
    assert found, f"{path.name}: нет константы TIER"
    assert found.group(1) in KNOWN_TIERS, (
        f"{path.name}: уровень {found.group(1)!r} вне схемы Гл. 4.2")


@pytest.mark.parametrize("path", replay_source_files(),
                         ids=lambda p: p.name)
def test_the_source_puts_its_declared_tier_into_the_matchref(path):
    """ГЛАВНОЕ: в MatchRef едет объявленная константа, а не литерал рядом.

    Именно на этом равенстве держится восстановление по имени источника.
    Разъедься объявление с тем, что источник кладёт в MatchRef, — и
    переотправленный матч получит уровень, которого у него никогда не
    было. Тихо: он просто окажется не в той половине датасета.

    Проверка текстовая, потому что построить настоящий источник нельзя
    без сети и базы. Ищется противоположное — литерал `tier="..."`
    вместо константы.
    """
    src = path.read_text(encoding="utf-8")
    literal = re.findall(r'tier="([^"]+)"', src)
    assert not literal, (
        f"{path.name}: уровень задан литералом {literal} в обход TIER — "
        f"константа и MatchRef разъедутся при первой же правке")
    assert "tier=self.TIER" in src, (
        f"{path.name}: MatchRef не получает объявленный TIER")


def test_a_real_source_yields_its_declared_tier():
    """То же равенство, но на живом объекте.

    Текстовая проверка выше видит форму записи, а не результат. Fixture —
    единственный источник, который строится без сети, и на нём равенство
    проверяется по-настоящему.
    """
    refs = list(FixtureSource().fetch_new(None))
    assert refs, "fixture ничего не отдал"
    assert {r.tier for r in refs} == {FixtureSource.TIER}


def test_the_registry_covers_every_replay_source():
    """В реестре ровно те источники, что дают реплей — ни больше, ни меньше.

    Недостающий источник — молчаливый пропуск его матчей при
    восстановлении. Лишний — имя, которого не бывает в базе, то есть
    правило, которое никогда не сработает и потому никогда не будет
    проверено.
    """
    from_files = set()
    for path in replay_source_files():
        src = path.read_text(encoding="utf-8")
        name = re.search(r'^\s*name = "([^"]+)"', src, re.M)
        assert name, f"{path.name}: у источника нет имени"
        from_files.add(name.group(1))
    assert set(replay_source_tiers()) == from_files


def test_the_registry_reports_the_declared_values():
    """Реестр отдаёт объявленные значения, а не свои.

    Второй список уровней внутри реестра выглядел бы точно так же и
    разъехался бы при первой правке источника.
    """
    tiers = replay_source_tiers()
    assert tiers["salts"] == "Pub"
    assert tiers["opendota"] == "Professional"
    assert tiers["opendota_public"] == "Premium"
    assert set(tiers.values()) <= KNOWN_TIERS


# -- разрешение уровня при восстановлении --------------------------------------

def resolve():
    """`resolve_tier` из инструмента; он лежит вне пакета."""
    sys.path.insert(0, str(SRC.parent / "tools"))
    from republish_orphans import resolve_tier

    return resolve_tier


BY_SOURCE = {"salts": "Pub", "opendota": "Professional"}


def test_the_mart_wins_over_the_source_name():
    """ГЛАВНОЕ: витрина приоритетнее имени источника.

    Не потому, что точнее, а потому, что её значение УЖЕ ЛЕЖИТ в
    обучающих данных. Витрина — ReplacingMergeTree, и событие с другим
    уровнем переписало бы строку задним числом: датасет менялся бы от
    запуска инструмента починки.
    """
    got = resolve()(42, "salts", {42: ("Premium", 57)}, BY_SOURCE)
    assert got == ("Premium", 57), (
        "имя источника перебило витрину — переотправка меняла бы уже "
        "записанный уровень")


def test_without_the_mart_the_source_name_decides():
    """Ради этого случая всё и делалось: витрины у сироты нет.

    Её пишет feature-extractor, то есть шаг, который у сироты не
    состоялся. Без этой ветки инструмент пропускал 361 матч из 510.
    """
    assert resolve()(42, "salts", {}, BY_SOURCE) == ("Pub", 0)


def test_the_fallback_says_the_patch_is_unknown():
    """Патч при этом ноль — «неизвестен», а не «наверное, последний».

    Ноль по контракту MatchRef и означает неизвестность, и обучение с ним
    умеет обращаться: вес по возрасту патча считается только для
    известных. Подстановка «последнего» соврала бы в поле, влияющем на
    веса, — ровно та беда, от которой уровень и оберегают.
    """
    _, patch = resolve()(42, "opendota", {}, BY_SOURCE)
    assert patch == 0


def test_an_unknown_source_is_not_guessed():
    """Незнакомый источник — пропуск, а не подстановка «Pub».

    Pub самый частый, и догадка почти всегда угадала бы. Почти: про-матч
    с ярлыком Pub уехал бы в обучение вместо эталона, и гейт мерил бы
    качество на данных, которые сам же и испортил.
    """
    assert resolve()(42, "новый_источник", {}, BY_SOURCE) is None
    assert resolve()(42, "", {}, BY_SOURCE) is None
    assert resolve()(42, None, {}, BY_SOURCE) is None
