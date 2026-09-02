"""Список источников — один на весь коллектор (спринт 180).

ЖИВОЙ СЛУЧАЙ 2026-09-02. Запуск нового источника ответил:

    __main__.py: error: unrecognized arguments
    --source {fixture,opendota,opendota-public,candidates,...}

В этом списке не было ни `salts`, ни `parked` — при том что фабрика их
создавала. Перечень имён существовал в двух местах и разъехался молча.

Держалось это на тонкости argparse: значение ПО УМОЛЧАНИЮ он по `choices`
не проверяет. Поэтому `COLLECTOR_SOURCE=parked` работал, а `--source
parked` отвергался — два способа задать одно и то же вели себя
по-разному, и `usage` показывал неполный список, то есть врал о
возможностях программы.
"""
import pathlib
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector.__main__ import SOURCES, build_source  # noqa: E402

MAIN = (SRC / "collector" / "__main__.py").read_text(encoding="utf-8")


def factory_names() -> set[str]:
    """Имена, которые РЕАЛЬНО обрабатывает фабрика.

    Извлекаются из самого кода, а не переписываются сюда: копия имела бы
    тот же изъян, что и `choices`, — она разъехалась бы молча.
    """
    names = set(re.findall(r'if name == "([a-z0-9-]+)"', MAIN))
    for group in re.findall(r'if name in \(([^)]+)\)', MAIN):
        names |= set(re.findall(r'"([a-z0-9-]+)"', group))
    return names


def test_the_extraction_finds_something():
    """Страховка от проверки пустоты.

    Сломайся разбор — сравнение ниже прошло бы на пустом множестве.
    """
    assert len(factory_names()) >= 8, factory_names()


def test_the_declared_list_matches_what_the_factory_builds():
    """ГЛАВНОЕ: объявленный список и фабрика знают одно и то же.

    Лишнее имя в списке — обещание источника, который упадёт при запуске.
    Недостающее — работающий источник, который нельзя выбрать флагом и
    которого нет в подсказке.
    """
    assert set(SOURCES) == factory_names()


@pytest.mark.parametrize("name", ["salts", "parked"])
def test_the_on_demand_sources_are_listed(name):
    """Источники «по требованию» тоже перечислены.

    Именно их и потеряли: они не поднимаются сервисом в compose, поэтому
    пропажу не видно до ручного запуска — а он случается редко и обычно
    тогда, когда что-то уже чинят.
    """
    assert name in SOURCES


def test_an_unknown_name_names_the_alternatives():
    """Отказ перечисляет доступные имена.

    COLLECTOR_SOURCE задаётся в env-файле и в compose, где опечатку
    глазами не поймать, а «unknown source 'slats'» без списка отправляет
    читать исходники.
    """
    with pytest.raises(ValueError) as exc:
        build_source("slats")
    assert "slats" in str(exc.value)
    assert "salts" in str(exc.value), "не показаны доступные источники"


def test_the_parser_takes_its_choices_from_the_single_list():
    """`choices` берётся из списка, а не переписывается рядом.

    Рукописный второй перечень — ровно то, что уже разъехалось.
    """
    assert "choices=SOURCES" in MAIN
    assert 'choices=["fixture"' not in MAIN
