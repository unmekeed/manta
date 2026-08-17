"""Тесты отчёта о ранговых полях в ответе GC (спринт 137).

Вопрос, на который отвечает отчёт, стоит дорого. Сейчас ранг каждого
игрока приезжает бесплатно вместе с солью из /matches/{id} OpenDota, и
на нём стоит измерение точности отбора: правило берёт матч по двум
известным рангам из десяти, и только факт показывает, не набираем ли мы
мусор. Если соль поедет из GC, этот источник истины исчезнет — если
только GC не отдаёт ранги сам.

Проверяется на НАСТОЯЩЕМ protobuf-сообщении, а не на заглушке: смысл
отчёта в том, какие поля есть у CMsgDOTAMatch и как они заполнены, и
подделка проверяла бы подделку.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

needs_dota2 = pytest.mark.skipif(
    importlib.util.find_spec("dota2") is None,
    reason="dota2 ставится в venv замера (make gc-venv)")


def _probe():
    """gc-probe.py импортируется по пути: в имени дефис, не модуль."""
    spec = importlib.util.spec_from_file_location(
        "gc_probe", SCRIPTS / "gc-probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["gc_probe"] = module
    spec.loader.exec_module(module)
    return module


def _match(**player_fields):
    from dota2.protobufs.dota_gcmessages_common_pb2 import CMsgDOTAMatch
    m = CMsgDOTAMatch()
    for _ in range(10):
        m.players.add()
    for name, values in player_fields.items():
        for player, value in zip(m.players, values):
            setattr(player, name, value)
    return m


@needs_dota2
def test_empty_ranks_are_reported_as_absent():
    """Пустые ранговые поля — это «ранга нет», а не «ноль игроков с рангом».

    Отличие не косметическое: пустой ответ означает, что истину по
    рангам придётся и дальше брать из OpenDota, то есть GC не снимает
    зависимость целиком.
    """
    probe = _probe()
    rep = probe.rank_report(_match())
    assert all(rep[name] == 0 for name in probe.RANK_FIELDS)
    assert rep["average_skill"] == 0


@needs_dota2
def test_filled_ranks_are_counted_per_player():
    probe = _probe()
    # Ранг известен у шести игроков из десяти — обычная картина для
    # публичных матчей, где часть профилей закрыта.
    rep = probe.rank_report(_match(previous_rank=[80, 80, 75, 0, 0, 0,
                                                  71, 70, 0, 80]))
    assert rep["previous_rank"] == 6
    assert rep["rank_change"] == 0


@needs_dota2
def test_average_skill_is_per_match_not_per_player():
    """average_skill — одно число на матч, и это не ранг.

    Три градации (normal/high/very high) против семи с половиной десятков
    у rank_tier. Спутать их значило бы решить, что отбор по Immortal
    можно строить на этом поле.
    """
    from dota2.protobufs.dota_gcmessages_common_pb2 import CMsgDOTAMatch
    probe = _probe()
    m = CMsgDOTAMatch()
    m.average_skill = 3
    for _ in range(10):
        m.players.add()
    assert probe.rank_report(m)["average_skill"] == 3
    assert all(probe.rank_report(m)[n] == 0 for n in probe.RANK_FIELDS)


@needs_dota2
def test_report_names_the_consequence_either_way(capsys):
    """Отчёт печатает ВЫВОД, а не только числа.

    Замер читают спустя недели; строка «=> ранг из GC достаётся» экономит
    восстановление контекста, а без неё числа пришлось бы толковать
    заново.
    """
    probe = _probe()

    probe.print_rank_report(_match())
    assert "рангов в ответе нет" in capsys.readouterr().out

    probe.print_rank_report(_match(previous_rank=[80] + [0] * 9))
    assert "ДОСТАЁТСЯ" in capsys.readouterr().out


@needs_dota2
def test_all_reported_fields_exist_in_the_protocol():
    """Каждое имя из RANK_FIELDS есть у игрока в CMsgDOTAMatch.

    Опечатка в имени дала бы вечный ноль, неотличимый от «Valve поле не
    заполняет» — то есть неверный вывод по проекту, а не падение.
    """
    from dota2.protobufs.dota_gcmessages_common_pb2 import CMsgDOTAMatch
    probe = _probe()
    player = CMsgDOTAMatch.DESCRIPTOR.nested_types_by_name["Player"]
    names = {f.name for f in player.fields}
    missing = [n for n in probe.RANK_FIELDS if n not in names]
    assert not missing, f"нет таких полей у игрока: {missing}"


# -- источник match_id для замера --------------------------------------------------

def test_public_feed_sends_a_user_agent():
    """OpenDota отбивает дефолтный «Python-urllib/3.x» кодом 403.

    Тот же запрос через curl проходит, поэтому ошибка выглядела бы как
    «лента недоступна» и уводила бы искать проблему в сети. Проверяем не
    сеть, а что заголовок вообще ставится.
    """
    import urllib.request

    probe = _probe()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        raise RuntimeError("дальше не идём — проверяем только заголовок")

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        probe.match_ids_from_public(10)
    finally:
        urllib.request.urlopen = original

    assert seen.get("ua"), "User-Agent не задан — OpenDota ответит 403"
    assert "urllib" not in seen["ua"].lower()


def test_public_feed_failure_is_not_silent(capsys):
    """Сеть отказала — говорим об этом, а не возвращаем пустоту молча.

    Молчаливый пустой список неотличим от «матчей нет», и замер сообщал
    бы «очередь пуста» там, где на самом деле не дотянулся до сети.
    """
    import urllib.request

    probe = _probe()

    def boom(req, timeout=None):
        raise OSError("сеть отвалилась")

    original = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        assert probe.match_ids_from_public(10) == []
    finally:
        urllib.request.urlopen = original
    assert "недоступна" in capsys.readouterr().out


def test_missing_psycopg_is_reported_not_swallowed(capsys, monkeypatch):
    """Нет psycopg — это «прочитать нечем», а не «очередь пуста».

    Ровно эта подмена смысла и случилась на живой машине: замер сообщил
    «очередь пуста» в тот момент, когда очередь он не умел прочитать —
    psycopg живёт в окружении коллекторов, а у замера своё.
    """
    import builtins

    probe = _probe()
    real_import = builtins.__import__

    def no_psycopg(name, *a, **kw):
        if name == "psycopg":
            raise ImportError("нет такого")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_psycopg)
    assert probe.match_ids_from_queue(10) == []
    out = capsys.readouterr().out
    assert "прочитать нечем" in out
    assert "--from-public" in out
