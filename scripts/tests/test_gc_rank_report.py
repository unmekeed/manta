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
    assert "РАНГОВ В ОТВЕТЕ НЕТ" in capsys.readouterr().out

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
    probe = _probe()
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["ua"] = (headers or {}).get("User-Agent")
        raise RuntimeError("дальше не идём — проверяем только заголовок")

    import requests
    original = requests.get
    requests.get = fake_get
    try:
        probe.match_ids_from_public(10)
    finally:
        requests.get = original

    assert seen.get("ua"), "User-Agent не задан — OpenDota ответит 403"
    assert "urllib" not in seen["ua"].lower()


def test_public_feed_failure_is_not_silent(capsys):
    """Сеть отказала — говорим об этом, а не возвращаем пустоту молча.

    Молчаливый пустой список неотличим от «матчей нет», и замер сообщал
    бы «очередь пуста» там, где на самом деле не дотянулся до сети.
    """
    probe = _probe()

    def boom(url, headers=None, timeout=None):
        raise OSError("сеть отвалилась")

    import requests
    original = requests.get
    requests.get = boom
    try:
        assert probe.match_ids_from_public(10) == []
    finally:
        requests.get = original
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


def test_public_feed_retries_before_giving_up(capsys):
    """Одна неудача — не приговор ленте.

    На живой машине ответ не пришёл за 30 секунд, хотя коллекторы к тому
    же API ходят успешно. Лента отдаёт сотню матчей целиком и бывает
    медленной, поэтому попыток две; объявлять её недоступной с первого
    таймаута значит останавливать замер из-за случайности.
    """
    probe = _probe()
    calls = []

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [{"match_id": 8951315555}]

    def flaky(url, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return FakeResp()

    import requests
    original = requests.get
    requests.get = flaky
    try:
        assert probe.match_ids_from_public(10) == [8951315555]
    finally:
        requests.get = original
    assert len(calls) == 2, "второй попытки не было"


def test_public_feed_points_at_the_valve_fallback(capsys):
    """Лента совсем не отвечает — замер называет обходной путь.

    Без этого пользователь упирается в «недоступна» и не знает, что у
    Valve есть тот же список, не тратящий квоту OpenDota.
    """
    probe = _probe()

    def dead(url, headers=None, timeout=None):
        raise TimeoutError("read timed out")

    import requests
    original = requests.get
    requests.get = dead
    try:
        assert probe.match_ids_from_public(10) == []
    finally:
        requests.get = original
    assert "--from-steam" in capsys.readouterr().out


def test_steam_source_needs_a_key_and_says_so(capsys, monkeypatch):
    probe = _probe()
    monkeypatch.delenv("STEAM_API_KEY", raising=False)
    assert probe.match_ids_from_steam(10) == []
    assert "STEAM_API_KEY" in capsys.readouterr().out


def test_steam_source_rejects_a_bad_status(capsys, monkeypatch):
    """status != 1 — это отказ Valve, а не пустой список матчей.

    Valve отвечает HTTP 200 и кладёт беду внутрь тела; принять это за
    «матчей нет» значило бы искать причину не там.
    """
    probe = _probe()
    monkeypatch.setenv("STEAM_API_KEY", "ключ")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"result": {"status": 15,
                                           "statusDetail": "ключ не годится"}}

    import requests
    original = requests.get
    requests.get = lambda *a, **kw: FakeResp()
    try:
        assert probe.match_ids_from_steam(10) == []
    finally:
        requests.get = original
    assert "status=15" in capsys.readouterr().out


def test_steam_source_returns_match_ids(monkeypatch):
    """Обычный случай: Valve отдала страницу, дальше пусто."""
    probe = _probe()
    monkeypatch.setenv("STEAM_API_KEY", "ключ")
    calls = []

    class FakeResp:
        def __init__(self, ids): self._ids = ids
        def raise_for_status(self): pass
        def json(self):
            return {"result": {"status": 1,
                               "matches": [{"match_id": i} for i in self._ids]}}

    def fake_get(url, params=None, timeout=None):
        calls.append(1)
        return FakeResp([8951315555, 8951315458] if len(calls) == 1 else [])

    import requests
    original = requests.get
    requests.get = fake_get
    try:
        assert probe.match_ids_from_steam(10) == [8951315555, 8951315458]
    finally:
        requests.get = original


# -- истолкование ответа GC --------------------------------------------------------

class _Resp:
    def __init__(self, result):
        self.result = result


@needs_dota2
def test_eresult_one_is_success_not_failure():
    """result == 1 — это УСПЕХ.

    Ошибка, стоившая целого живого прогона: успехом считался ноль, а
    `result` это EResult, где успех — единица, а ноль означает Invalid.
    Замер записывал каждый нормальный ответ GC в отказы, останавливался
    после трёх подряд и объявлял «аккаунт отдал 0 солей, нужно 2000
    аккаунтов» — притом что GC отвечал исправно.
    """
    probe = _probe()
    assert probe.detail_failure_reason(_Resp(1)) is None


@needs_dota2
def test_eresult_zero_is_a_failure():
    """Ноль — это Invalid, а не успех."""
    probe = _probe()
    assert probe.detail_failure_reason(_Resp(0)) is not None


@needs_dota2
def test_silence_is_a_failure_with_its_own_name():
    probe = _probe()
    assert probe.detail_failure_reason(None) == "молчание GC"


@needs_dota2
def test_failure_reason_is_named_not_numbered():
    """Причина отказа читается словами, а не кодом.

    «eresult=15» требует лезть в таблицу; «AccessDenied» отвечает сразу
    и, в частности, сразу отличило бы запрет для limited-аккаунта от
    отсутствия матча.
    """
    probe = _probe()
    from steam.enums import EResult
    reason = probe.detail_failure_reason(_Resp(int(EResult.AccessDenied)))
    assert "AccessDenied" in reason


@needs_dota2
def test_mmr_type_alone_does_not_mean_rank_is_available():
    """Ровно та ошибка, что случилась на живом прогоне.

    В первом же матче единственным ненулевым полем оказался `mmr_type`,
    и вердикт «любой ненуль => ранг достаётся» объявил, что OpenDota для
    истины по рангам больше не нужна. Это неправда: `mmr_type` говорит,
    КАКОЙ рейтинг считался (соло/пати), и ничего — о его величине.

    Правило, которое тест закрепляет: в вердикт идут поля со ЗНАЧЕНИЕМ
    ранга, а не поля, лежащие рядом по смыслу.
    """
    probe = _probe()
    rep = probe.rank_report(_match(mmr_type=[1] + [0] * 9))
    assert rep["mmr_type"] == 1, "поле всё ещё показывается"
    assert not probe.rank_is_available(rep), "mmr_type выдан за ранг"


@needs_dota2
def test_real_rank_field_does_mean_rank_is_available():
    probe = _probe()
    rep = probe.rank_report(_match(previous_rank=[80] + [0] * 9))
    assert probe.rank_is_available(rep)


@needs_dota2
def test_context_fields_are_shown_but_never_decide(capsys):
    """Соседние поля видно в отчёте, но вердикт они не двигают."""
    probe = _probe()
    for name in probe.RANK_CONTEXT_FIELDS:
        rep = probe.rank_report(_match(**{name: [1] * 10}))
        assert rep[name] == 10, name
        assert not probe.rank_is_available(rep), name

    probe.print_rank_report(_match(mmr_type=[1] * 10))
    out = capsys.readouterr().out
    assert "mmr_type" in out
    assert "РАНГОВ В ОТВЕТЕ НЕТ" in out


@needs_dota2
def test_rank_and_context_fields_do_not_overlap():
    """Одно поле не может быть и решающим, и справочным."""
    probe = _probe()
    assert not (set(probe.RANK_FIELDS) & set(probe.RANK_CONTEXT_FIELDS))


@needs_dota2
def test_all_context_fields_exist_in_the_protocol():
    from dota2.protobufs.dota_gcmessages_common_pb2 import CMsgDOTAMatch
    probe = _probe()
    player = CMsgDOTAMatch.DESCRIPTOR.nested_types_by_name["Player"]
    names = {f.name for f in player.fields}
    missing = [n for n in probe.RANK_CONTEXT_FIELDS if n not in names]
    assert not missing, f"нет таких полей у игрока: {missing}"


# -- постраничность и вердикт ------------------------------------------------------

def test_steam_source_pages_beyond_one_hundred(monkeypatch):
    """За вызов Valve отдаёт сотню — просят больше, значит листаем.

    Первый живой прогон обработал ровно 100 матчей БЕЗ единого отказа:
    упёрся не в потолок аккаунта, а в конец списка. Без страниц суточный
    потолок измерить нечем.
    """
    probe = _probe()
    monkeypatch.setenv("STEAM_API_KEY", "ключ")
    pages, seen = [], []

    class FakeResp:
        def __init__(self, ids): self._ids = ids
        def raise_for_status(self): pass
        def json(self):
            return {"result": {"status": 1,
                               "matches": [{"match_id": i} for i in self._ids]}}

    def fake_get(url, params=None, timeout=None):
        seen.append(params.get("start_at_match_id"))
        base = 9000 - len(seen) * 100
        ids = list(range(base + 99, base - 1, -1))
        pages.append(ids)
        return FakeResp(ids)

    import requests
    original = requests.get
    requests.get = fake_get
    try:
        ids = probe.match_ids_from_steam(250)
    finally:
        requests.get = original

    assert len(ids) == 250, f"вернулось {len(ids)}"
    assert len(seen) == 3, "должно быть три страницы"
    assert seen[0] is None, "первая страница идёт без start_at_match_id"
    # Каждая следующая страница начинается ПЕРЕД последним матчем прошлой.
    assert seen[1] == min(pages[0]) - 1
    assert seen[2] == min(pages[1]) - 1


def test_steam_source_stops_when_valve_runs_out(monkeypatch):
    """Пустая страница — конец, а не повод крутиться вечно."""
    probe = _probe()
    monkeypatch.setenv("STEAM_API_KEY", "ключ")
    calls = []

    class FakeResp:
        def __init__(self, ids): self._ids = ids
        def raise_for_status(self): pass
        def json(self):
            return {"result": {"status": 1,
                               "matches": [{"match_id": i} for i in self._ids]}}

    def fake_get(url, params=None, timeout=None):
        calls.append(1)
        return FakeResp([9001, 9000] if len(calls) == 1 else [])

    import requests
    original = requests.get
    requests.get = fake_get
    try:
        assert probe.match_ids_from_steam(500) == [9001, 9000]
    finally:
        requests.get = original
    assert len(calls) == 2, "после пустой страницы надо остановиться"
