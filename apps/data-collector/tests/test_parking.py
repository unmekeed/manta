"""Недостижимый реплей паркуется, а не теряется (спринт 153).

ЧТО СЛУЧИЛОСЬ. The International 2026 играется на китайском кластере
Valve: одиннадцать из двенадцати последних про-матчей раздаются с
replay413.dota2.com.cn. Ни VPS в Ирландии, ни домашняя машина до него не
доходят — проверено с обеих, соединение просто истекает.

Коллектор вёл себя ровно как велено в спринте 118: три попытки, потом
«сдвигаю курсор, чтобы не блокировать очередь». Правило верное — вставший
курсор в 2026-07-31 стоил 82 часов простоя. Но у него оказалась
неназванная цена: курсор монотонный, и сдвинутый матч не вернётся
НИКОГДА. Появись маршрут завтра, реплеи TI всё равно потеряны.

ЧТО ПРОВЕРЯЕТСЯ. Два решения, которые раньше были одним: очередь идёт
дальше (как и шла), но матч при этом ЗАПОМИНАЕТСЯ. Плюс то, что мы
перестаём набирать номер хоста, который дважды не ответил, — иначе
восемьдесят секунд на матч съедали бы цикл, а перед очередью сотни
матчей одного турнира на одном хосте.

Запросы к базе здесь не проверяются: их смысл живёт в SQL, и фейковый
курсор проверял бы сам себя. Для них есть tests/test_parking_sql.py на
живой базе.
"""
import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.parked import (UnreachableHosts,  # noqa: E402
                              is_unreachable, replay_host)
from collector.runner import MAX_TRANSIENT_RETRIES  # noqa: E402
from collector.sources import IncompleteDownloadError  # noqa: E402
from test_collector_cursor import FakeSource, make_collector  # noqa: E402

CN = "http://replay413.dota2.com.cn/570/1_2.dem.bz2"


def _timeout(host="replay413.dota2.com.cn"):
    return requests.exceptions.ConnectTimeout(
        f"Connection to {host} timed out. (connect timeout=10.0)")


# -- признак недостижимости ---------------------------------------------------

def test_connect_timeout_is_unreachable():
    """Ровно то исключение, что пришло с VPS."""
    assert is_unreachable(_timeout()) is True


@pytest.mark.parametrize("exc", [
    requests.exceptions.ReadTimeout("данные кончились"),
    requests.exceptions.ChunkedEncodingError("обрыв на середине"),
    requests.exceptions.HTTPError("503"),
    ValueError("наш баг"),
])
def test_other_failures_are_not_unreachability(exc):
    """Узко и намеренно.

    Обрыв в середине передачи, 5xx и наша собственная ошибка означают
    разное, и часть из них временная. Запиши мы их в недостижимость —
    парковали бы матчи, которые взялись бы со второй попытки, и вдобавок
    заклеймили бы живой хост.
    """
    assert is_unreachable(exc) is False


def test_host_is_extracted_from_the_url():
    assert replay_host(CN) == "replay413.dota2.com.cn"
    assert replay_host("http://replay181.valve.net/570/x.bz2") == "replay181.valve.net"
    assert replay_host("") == ""
    assert replay_host(None) == ""


# -- отметка о хосте ----------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_one_failure_is_not_enough():
    """Одна неудача — ещё не приговор хосту.

    Порог в две попытки не формальность: единичный обрыв случается и у
    живого сервера, а заклеймённый хост перестаёт опрашиваться на часы.
    """
    hosts = UnreachableHosts(threshold=2, clock=_Clock())
    assert hosts.record_failure("h") is False
    assert hosts.is_unreachable("h") is False


def test_second_failure_marks_the_host():
    hosts = UnreachableHosts(threshold=2, clock=_Clock())
    hosts.record_failure("h")
    assert hosts.record_failure("h") is True
    assert hosts.is_unreachable("h") is True


def test_mark_expires():
    """Отметка со сроком годности: маршруты меняются.

    Вечная превратила бы временную беду в постоянную, и заметить это было
    бы нечем — в логе ровным счётом ничего не происходит.
    """
    clock = _Clock()
    hosts = UnreachableHosts(threshold=1, ttl_s=100, clock=clock)
    hosts.record_failure("h")
    assert hosts.is_unreachable("h") is True
    clock.t = 100
    assert hosts.is_unreachable("h") is False


def test_expiry_gives_a_clean_slate():
    """После срока хост начинает с нуля, а не с порога.

    Иначе одна старая неудача вечно держала бы его в одном шаге от
    клейма, и первый же обрыв возвращал бы отметку на часы.
    """
    clock = _Clock()
    hosts = UnreachableHosts(threshold=2, ttl_s=100, clock=clock)
    hosts.record_failure("h")
    hosts.record_failure("h")
    clock.t = 100
    assert hosts.is_unreachable("h") is False
    assert hosts.record_failure("h") is False, "старые неудачи не забыты"


def test_success_clears_everything():
    hosts = UnreachableHosts(threshold=2, clock=_Clock())
    hosts.record_failure("h")
    hosts.record_success("h")
    assert hosts.record_failure("h") is False, "неудачи считаются заново"


def test_empty_host_is_never_marked():
    """Неразобранный адрес не должен клеймить «пустой хост».

    Иначе один матч с испорченной ссылкой заблокировал бы все остальные
    с такими же — то есть беду данных мы приняли бы за беду сети.
    """
    hosts = UnreachableHosts(threshold=1, clock=_Clock())
    assert hosts.record_failure("") is False
    assert hosts.is_unreachable("") is False


# -- поведение цикла ----------------------------------------------------------

def test_exhausted_match_is_parked_not_lost():
    """ГЛАВНОЕ утверждение спринта.

    Курсор уходит вперёд — как и раньше, очередь важнее одного матча. Но
    матч теперь записан, и к нему можно вернуться.
    """
    src = FakeSource([500], fail={500: _timeout()})
    c = make_collector(src)
    for _ in range(MAX_TRANSIENT_RETRIES):
        c.collect_once()
    assert c.cursor == "500", "очередь встала — вернулась беда 2026-07-31"
    assert [m for m, _ in c.parked] == [500], "матч потерян молча"


def test_exhausted_non_network_failure_is_also_parked():
    """Паркуется ЛЮБОЙ матч, исчерпавший попытки, а не только сетевой.

    Мутационная проверка вскрыла дыру в этом файле: первый тест парковки
    гонял три цикла с ConnectTimeout, и к третьему хост успевал получить
    клеймо — матч уходил в парковку по ветке «хост недостижим», а не по
    ветке «попытки кончились». Убери вторую вовсе — тест остался бы
    зелёным.

    Здесь ошибка НЕ сетевая (TimeoutError, а не ConnectTimeout), хост
    клейма не получает, и сработать может только ветка исчерпания. Матч
    при этом теряется так же безвозвратно, как и недостижимый: курсор
    монотонный.
    """
    src = FakeSource([550], fail={550: TimeoutError("сеть")})
    c = make_collector(src)
    for _ in range(MAX_TRANSIENT_RETRIES):
        c.collect_once()
    assert c._unreachable.is_unreachable("x") is False, "хост зря заклеймён"
    assert [m for m, _ in c.parked] == [550], "матч потерян молча"
    assert c.cursor == "550"


def test_success_between_failures_clears_the_host_counter():
    """Успех обнуляет счётчик неудач хоста.

    Без этого две неудачи, разнесённые по времени и разделённые
    успешными скачиваниями, копились бы до клейма — и живой сервер,
    икнувший дважды за сутки, замолчал бы на часы.

    Мутация «не звать record_success» пережила первый заход: в
    единственном тесте, который её касался, до порога всё равно не
    доходило.
    """
    c = make_collector(FakeSource([800, 801, 802],
                                  fail={800: _timeout(), 802: _timeout()}))
    c.collect_once()          # 800 упал (1), 801 собрался — счётчик сброшен,
                              # 802 упал (1 после сброса)
    assert c._unreachable.is_unreachable("x") is False, (
        "хост заклеймён двумя неудачами, разделёнными успехом")


def test_truncated_download_is_parked_too():
    """Оборванная передача, исчерпавшая попытки, тоже паркуется.

    У неё своя ветка обработки — файл на сервере цел, оборвалась
    передача, — и в первой версии спринта 153 парковки там не оказалось:
    матч уходил вперёд по курсору и терялся так же безвозвратно, как
    недостижимый.

    Нашла это мутационная проверка, промахнувшаяся мимо соседней ветки:
    искомый текст встречался в файле дважды, и правка легла не туда, где
    я её ждал. Промах оказался полезнее попадания.
    """
    src = FakeSource([560], fail={
        560: IncompleteDownloadError("скачано 40 из 90 МиБ")})
    c = make_collector(src)
    for _ in range(MAX_TRANSIENT_RETRIES):
        c.collect_once()
    assert [m for m, _ in c.parked] == [560], "матч потерян молча"
    assert c.cursor == "560", "очередь встала"


def test_match_is_not_parked_while_retries_remain():
    """Пока попытки есть, парковать рано: матч ещё может взяться."""
    src = FakeSource([501], fail={501: _timeout()})
    c = make_collector(src)
    c.collect_once()
    assert c.parked == []


def test_known_dead_host_costs_no_attempt():
    """Второй матч с мёртвого хоста не набирает его номер.

    Восемьдесят секунд на матч — это DNS, отдающий несколько адресов, и
    каждый пробуется по таймауту соединения. При сотнях матчей одного
    турнира на одном хосте очередь двигалась на два матча за три часа.
    """
    src = FakeSource([600, 601], fail={600: _timeout(), 601: _timeout()})
    c = make_collector(src)
    # Порог 2: две неудачи на первом матче клеймят хост.
    c.collect_once()
    c.collect_once()
    src.downloads.clear()
    c.collect_once()
    assert src.downloads == [], "к мёртвому хосту снова ходили"
    assert {m for m, _ in c.parked} >= {600, 601}, "матчи не припаркованы"
    assert c.cursor == "601", "очередь не двинулась"


def test_a_reachable_host_is_untouched():
    """Клеймо не расползается на соседние хосты.

    FakeSource выдаёт адреса вида http://x/<id>.bz2 — то есть ОДИН хост
    на все матчи. Здесь важно, что успех этот хост очищает: иначе первая
    же пара обрывов у живого сервера остановила бы сбор на часы.
    """
    src = FakeSource([700, 701], fail={700: _timeout()})
    c = make_collector(src)
    c.collect_once()                      # 700 упал, 701 собрался
    assert b"match_id:701" in c.published
    assert c._unreachable.is_unreachable("x") is False
