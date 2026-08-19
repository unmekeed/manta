"""Таймаут соединения и таймаут данных — разные величины (спринт 150).

ЧТО СЛУЧИЛОСЬ. За сутки работы VPS не разобрал ни одного реплея:
PositionSnapshots пуст, карт нет ни одной. Выглядело это как «сломались
карты», а сломана была сеть — и починка стоила одной строки.

    10:16:14  downloading http://replay413.dota2.com.cn/570/8943091110…
    10:34:17  download failed (ConnectTimeoutError), попытка 1/3

Восемнадцать минут на матч. Реплеи раздаёт региональный CDN Valve, и
китайский кластер с ирландского адреса недостижим; в коде стояло
`requests.get(url, timeout=600.0)`, а одно число requests применяет И к
установке соединения, И к ожиданию данных. То есть десять минут на TCP-
рукопожатие, которое с живым хостом занимает миллисекунды.

Три попытки на матч перед сдвигом курсора — почти час на ОДИН
недостижимый матч. Коллектор не успевал дойти до скачиваемых.

ПОЧЕМУ ЭТО НЕ ЛОВИЛОСЬ. Дома реплеи качались: домашний адрес до
китайского кластера доходит. Величина, от которой всё зависело, ни разу
не проверялась — она и не выглядела величиной, а выглядела запасом «на
всякий случай». Запас и оказался ловушкой: чем он больше, тем дольше
неудача притворяется работой.
"""
import pathlib
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from collector.sources.opendota import (OpenDotaSource,  # noqa: E402
                                        REPLAY_CONNECT_TIMEOUT_S,
                                        REPLAY_READ_TIMEOUT_S)
from collector.sources import MatchRef  # noqa: E402
from collector.sources.opendota import IncompleteDownloadError  # noqa: E402


class _Resp:
    status_code = 200
    headers: dict = {}
    content = b""

    def raise_for_status(self):
        pass


def _ref():
    return MatchRef(match_id=1, replay_url="http://example.invalid/1.dem.bz2",
                    tier="Professional", source_cursor="0")


def test_download_passes_separate_connect_and_read_timeouts(monkeypatch):
    """ГЛАВНОЕ утверждение спринта: таймаут — пара, а не одно число.

    Скаляр requests применяет к обеим фазам. Проверять надо именно то,
    ЧТО УХОДИТ в requests: константы могут быть безупречны и не
    использоваться.
    """
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr("collector.sources.opendota.requests.get", fake_get)
    src = OpenDotaSource()
    # Ловим ИМЕННО ту ошибку, которой ждём. Первая версия теста писала
    # `pytest.raises(Exception)` — и проглотила TypeError от неверно
    # собранного MatchRef, из-за чего requests.get не вызывался вовсе, а
    # тест сообщал «таймаут задан одним числом». Диагноз был бы неверный:
    # широкий except в проверке врёт так же, как в коде.
    with pytest.raises(IncompleteDownloadError):
        src.download_replay(_ref())

    assert isinstance(seen.get("timeout"), tuple), (
        "таймаут задан одним числом — requests применит его и к соединению, "
        "и к данным")
    connect, read = seen["timeout"]
    assert (connect, read) == (REPLAY_CONNECT_TIMEOUT_S, REPLAY_READ_TIMEOUT_S)


def test_connect_timeout_is_seconds_not_minutes():
    """Рукопожатие с живым хостом занимает миллисекунды.

    Верхняя граница здесь не вкусовая: при трёх попытках на матч
    полминуты на соединение означают полторы минуты на недостижимый матч.
    Минута и больше вернула бы прежнюю беду в мягком виде — коллектор
    снова тратил бы часы на хосты, которые не ответят.
    """
    assert 1.0 <= REPLAY_CONNECT_TIMEOUT_S <= 30.0


def test_read_timeout_stays_generous():
    """А вот ожидание ДАННЫХ укорачивать нельзя.

    Реплей весит 50–110 МиБ, раздаётся медленно, и у requests это таймаут
    между байтами, а не на всю передачу. Урезав его заодно с первым, мы
    поменяли бы одну поломку на другую: живые скачивания начали бы
    обрываться на середине и выглядеть как битые архивы.
    """
    assert REPLAY_READ_TIMEOUT_S >= 300.0


# -- правило, а не случай -----------------------------------------------------

CALL_RE = re.compile(r"requests\.(?:get|post|put|head)\((.*?)\)", re.S)
SCALAR_TIMEOUT_RE = re.compile(r"timeout\s*=\s*([0-9]+(?:\.[0-9]+)?)")

# Выше этого числа скаляр почти наверняка задуман как ожидание ДАННЫХ, а
# значит молча становится и таймаутом соединения. Порог с запасом: самый
# длинный оставшийся скаляр в проекте — 120 секунд у бэкфилла, и он
# осмысленно долгий (страница таймлайна собирается на стороне OpenDota).
MAX_SCALAR_TIMEOUT_S = 120.0


def collector_sources() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def test_the_scanner_sees_the_download_call():
    """Страховка от проверки пустоты.

    Сломайся разбор — правило ниже перестало бы находить вызовы и стало
    бы вечно зелёным, в точности как оно отсутствовало до этого спринта.
    """
    text = (SRC / "collector" / "sources" / "opendota.py").read_text(
        encoding="utf-8")
    calls = CALL_RE.findall(text)
    assert any("replay_url" in c for c in calls), "разбор вызовов сломан"
    assert SCALAR_TIMEOUT_RE.search("requests.get(u, timeout=600.0)")
    assert not SCALAR_TIMEOUT_RE.search("requests.get(u, timeout=(10, 600))")


@pytest.mark.parametrize("path", collector_sources(), ids=lambda p: p.name)
def test_no_request_hides_a_long_connect_timeout(path):
    """Длинное ожидание задаётся ПАРОЙ, а не одним числом.

    Правило, а не случай: то же самое можно написать в любом новом
    источнике, и обойдётся оно теми же часами простоя. Скаляр допустим
    ровно пока он короткий — тогда он безвреден в обеих ролях.
    """
    text = path.read_text(encoding="utf-8")
    bad = []
    for args in CALL_RE.findall(text):
        m = SCALAR_TIMEOUT_RE.search(args)
        if m and float(m.group(1)) > MAX_SCALAR_TIMEOUT_S:
            bad.append(f"timeout={m.group(1)}")
    assert not bad, (
        f"{path.name}: {bad} — одно число requests применит и к соединению; "
        f"задайте пару (соединение, данные)")
