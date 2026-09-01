"""Замер GC входит через ДОСТУПНЫЕ серверы Steam (спринт 164).

ЧТО СЛУЧИЛОСЬ. `make gc-probe ARGS=login` на VPS стоял десять минут на
строке «вход по токену …» и не говорил ничего. Библиотека `steam`
спрашивает у Valve список серверов входа и получает адреса на порту
27017; с этой машины не отвечает ни один из двадцати четырёх — проверено
поимённо. Отказа при этом нет: `steam` молча перебирает список и
переподключается, сколько угодно долго.

Сеть при этом целая. Node с той же машины в Steam входит (`make gc-token`
прошёл), и TCP до CM на портах 27018 и 27019 устанавливается. Живой
опыт: `merge_list` с тремя проверенными адресами → `connect → True`.

Мертва не сеть и не аккаунт — мёртв ровно тот набор адресов, который
библиотека выбирает себе сама.

ЧТО ПРОВЕРЯЕТСЯ. Отбор адресов: оба списка Valve, выброшенный 443,
остановка после нужного числа живых и поведение, когда Valve не отвечает.
Сеть здесь не нужна и не используется — и запрос, и проверка
достижимости подставляются.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts" / "gc-probe.py"


def _load():
    """Модуль с дефисом в имени — только через importlib."""
    spec = importlib.util.spec_from_file_location("gc_probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gc_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


gc = _load()


def directory(netfilter=(), websockets=()):
    """Подставной ответ Valve: два списка по разновидностям."""
    def fetch(url):
        rows = websockets if "websockets" in url else netfilter
        return {"response": {"serverlist": [{"endpoint": e} for e in rows]}}
    return fetch


# -- отбор кандидатов ----------------------------------------------------------

def test_both_lists_are_asked():
    """Оба списка, а не только «свой».

    Сырому протоколу полагался бы netfilter — но на живой машине он ожил
    на портах из вебсокетного списка. Спроси мы один, отбросили бы
    единственное работающее.
    """
    fetch = directory(netfilter=["1.1.1.1:27017"],
                      websockets=["cm1.example.net:27018"])
    got = gc.cm_candidates(fetch=fetch)
    assert ("1.1.1.1", 27017) in got
    assert ("cm1.example.net", 27018) in got


def test_wss_port_is_dropped():
    """443 — это WebSocket, а библиотека говорит сырым протоколом.

    Оставь мы 443 в списке — клиент подключился бы к слушателю, который
    его не понимает, и повис бы там же, где висел до сих пор. Отличить
    это от прежней беды было бы нечем.
    """
    fetch = directory(websockets=["cm1.example.net:443",
                                  "cm1.example.net:27018"])
    got = gc.cm_candidates(fetch=fetch)
    assert ("cm1.example.net", 443) not in got
    assert ("cm1.example.net", 27018) in got


def test_duplicates_are_collapsed():
    """Один адрес встречается в обоих списках — брать его дважды незачем."""
    fetch = directory(netfilter=["1.1.1.1:27018"], websockets=["1.1.1.1:27018"])
    assert gc.cm_candidates(fetch=fetch) == [("1.1.1.1", 27018)]


def test_a_silent_directory_does_not_break_the_other():
    """Один список молчит — второй всё равно используется.

    Valve отвечает не всегда, и отказ целиком из-за одной неудачной
    половины вернул бы нас к десятиминутному молчанию.
    """
    def fetch(url):
        if "netfilter" in url:
            raise OSError("Valve молчит")
        return {"response": {"serverlist": [{"endpoint": "1.1.1.1:27018"}]}}
    assert gc.cm_candidates(fetch=fetch) == [("1.1.1.1", 27018)]


@pytest.mark.parametrize("bad", ["", "нет-порта", "host:порт", "host:"])
def test_broken_endpoints_are_ignored(bad):
    """Мусор в ответе не должен ронять вход.

    Свалиться здесь значило бы променять «висит молча» на «падает на
    ровном месте» — обмен одной беды на другую.
    """
    fetch = directory(netfilter=[bad, "1.1.1.1:27018"])
    assert gc.cm_candidates(fetch=fetch) == [("1.1.1.1", 27018)]


# -- проверка достижимости -----------------------------------------------------

def test_only_reachable_addresses_survive():
    """ГЛАВНОЕ утверждение спринта: недоступные отсеиваются ЗДЕСЬ.

    Именно их библиотека и получала — двадцать четыре штуки, все мёртвые.
    """
    alive = {("живой", 27018): "9.9.9.9"}
    probe = lambda h, p: alive.get((h, p))
    got = gc.reachable_cms([("мёртвый", 27017), ("живой", 27018)], probe=probe)
    assert got == [("9.9.9.9", 27018)]


def test_addresses_are_resolved_to_ip():
    """В список клиента уходит IP, а не имя.

    Живой опыт, на котором всё и сошлось, делался на разрешённых
    адресах. Имя тоже может сработать, но проверено не оно.
    """
    probe = lambda h, p: "5.5.5.5"
    assert gc.reachable_cms([("cm1.example.net", 27018)], probe=probe) \
        == [("5.5.5.5", 27018)]


def test_search_stops_at_the_limit():
    """Обход прекращается, набрав нужное число.

    В списке бывает сорок адресов по три секунды таймаута. Полный обход
    стоил бы двух минут ПЕРЕД КАЖДЫМ входом — то есть лечение оказалось
    бы немногим быстрее болезни.
    """
    tried = []

    def probe(h, p):
        tried.append(h)
        return "1.2.3.4"

    got = gc.reachable_cms([(f"cm{i}", 27018) for i in range(20)],
                           probe=probe, limit=3)
    assert len(got) == 3
    assert len(tried) == 3, f"опрошено лишнее: {tried}"


# -- засев клиента -------------------------------------------------------------

class FakeList:
    def __init__(self):
        self.items = ["старое-мёртвое"]
        self.cleared = False

    def clear(self):
        self.items = []
        self.cleared = True

    def merge_list(self, pairs):
        self.items.extend(pairs)


class FakeClient:
    def __init__(self):
        self.cm_servers = FakeList()


def test_seeding_replaces_the_library_list(monkeypatch):
    """Список ЗАМЕНЯЕТСЯ, а не дополняется.

    Оставь мы прежние двадцать четыре мёртвых адреса рядом с живыми —
    клиент так же начал бы с них и так же завис бы, просто не всегда.
    Плавающее зависание хуже постоянного: его нечем поймать.
    """
    monkeypatch.setattr(gc, "reachable_cms", lambda *a, **k: [("1.2.3.4", 27018)])
    client = FakeClient()
    assert gc.seed_cm_servers(client, candidates=[("x", 27018)]) == 1
    assert client.cm_servers.cleared
    assert client.cm_servers.items == [("1.2.3.4", 27018)]


def test_nothing_reachable_leaves_the_client_untouched(monkeypatch):
    """Не нашли ничего — не трогаем клиента вовсе.

    Пустой список после `clear()` — это вход, которому некуда идти:
    отказ вместо попытки. Пусть библиотека пробует по-своему, а замер
    скажет, что засева не было.
    """
    monkeypatch.setattr(gc, "reachable_cms", lambda *a, **k: [])
    client = FakeClient()
    assert gc.seed_cm_servers(client, candidates=[("x", 27018)]) == 0
    assert not client.cm_servers.cleared
    assert client.cm_servers.items == ["старое-мёртвое"]


def test_login_seeds_before_connecting():
    """Засев стоит ДО входа, а не после.

    Порядок здесь и есть всё содержание правки: после входа он бесполезен.
    """
    src = PROBE.read_text(encoding="utf-8")
    assert "seed_cm_servers(steam)" in src
    assert src.index("seed_cm_servers(steam)") < src.index("login_with_token(steam")
