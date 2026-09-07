"""Постоянная ошибка осмотра — это вердикт, а не невезение (спринт 197).

ЖИВОЙ ЗАМЕР 07.09.2026. В каждом цикле `timeline-collector` из 28
осмотров 2–12 кончались «ошибкой запроса». Осмотр — самый дорогой вызов
цикла: ранг и режим матча известны только ПОСЛЕ `/matches/{id}`, поэтому
за каждого кандидата мы платим независимо от того, подойдёт он или нет.
До 40% самого дефицитного ресурса уходило в отказ.

ПОЧЕМУ ЭТО БЫЛО ДОРОЖЕ, ЧЕМ ВЫГЛЯДЕЛО. Отвергнутый фильтром кандидат
попадает в `_rejected` и второй раз не осматривается — кэш заведён именно
затем, «чтобы каждый цикл заново не платить detail-вызов за те же
match_id с вершины /parsedMatches». А вот матч, на котором OpenDota
ответила 404, туда НЕ попадал. Его осматривали снова каждые полчаса, пока
он не уползёт из окна листинга, и каждый раз это стоило полноценного
вызова.

404 — самый окончательный вердикт из возможных: матча нет и не будет. Он
обязан отправлять кандидата в отказы ровно так же, как «низкий ранг».

ТА ЖЕ ОШИБКА УЖЕ БЫЛА. 2026-07-31 реплейный путь стоял 82 часа, потому
что постоянный отказ скачивания обрабатывался как временный; лечилось
`PermanentDownloadError`. Здесь то же самое различие, только на другом
пути, и урок к нему не применили.

ВХОД БОЕВОЙ. Заглушка поднимает НАСТОЯЩИЙ `requests.HTTPError` с
настоящим `requests.Response` — ровно то, что порождает
`raise_for_status()`. Самодельное исключение с полем `status_code`
проверяло бы обработчик, которого в бою не существует.
"""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources.opendota_timeline import (  # noqa: E402
    PERMANENT_DETAIL_STATUSES, OpenDotaTimelineSource)

from test_timeline import _parsed_match  # noqa: E402


def http_error(code: int) -> requests.HTTPError:
    """Ошибка ровно того вида, что поднимает `raise_for_status()`."""
    resp = requests.Response()
    resp.status_code = code
    resp.url = "https://api.opendota.com/api/matches/1"
    return requests.HTTPError(f"{code} Error", response=resp)


def source_where(mid_to_error: dict[int, object], candidates=(3, 2, 1)):
    """Источник, у которого часть кандидатов отвечает ошибкой.

    Возвращает (источник, список осмотренных id) — список общий для всех
    циклов, поэтому по нему видно повторную оплату.
    """
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0)
    looked_at: list[int] = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    pages: list[int] = []

    def fake_get(path, **params):
        if path == "parsedMatches":
            # Одна страница, дальше пусто. Отдавать те же id на каждый
            # запрос — не экономия на фикстуре, а другая система: живой
            # листинг конечен, и источник, крутящий его вечно, осматривал
            # бы одних и тех же кандидатов внутри ОДНОГО цикла. Проверка
            # «платим ли повторно между циклами» на такой заглушке ничего
            # бы не значила.
            pages.append(1)
            return FakeResp([{"match_id": m} for m in candidates]
                            if len(pages) == 1 else [])
        mid = int(path.split("/")[1])
        looked_at.append(mid)
        err = mid_to_error.get(mid)
        if err is not None:
            raise err
        return FakeResp(_parsed_match(mid=mid))

    src._get = fake_get
    src._pages = pages          # чтобы каждый цикл начинал листинг заново
    return src, looked_at


def drain(src, times=2):
    """Прогнать N циклов. Каждый цикл — свежая страница листинга."""
    for _ in range(times):
        src._pages.clear()
        list(src.fetch_new(skip=lambda mid: False))


def test_the_fixture_really_reaches_the_detail_call():
    """Страховка от проверки пустоты.

    Не дойди цикл до осмотра — все проверки ниже сравнивали бы пустые
    списки и были бы тождественно зелёными.
    """
    src, looked_at = source_where({})
    drain(src, times=1)
    assert looked_at, "цикл не дошёл до /matches/{id}"


@pytest.mark.parametrize("code", sorted(PERMANENT_DETAIL_STATUSES))
def test_a_permanent_error_is_paid_for_once(code):
    """ГЛАВНОЕ: за матч с окончательной ошибкой платим ОДИН раз.

    404 значит «такого матча нет и не будет». Осматривать его повторно —
    это тратить самый дорогой вызов цикла на заведомо известный ответ,
    каждые полчаса, пока кандидат не уползёт из листинга.
    """
    src, looked_at = source_where({2: http_error(code)})
    drain(src, times=3)
    assert looked_at.count(2) == 1, (
        f"матч с HTTP {code} осмотрен {looked_at.count(2)} раза — "
        f"постоянная ошибка не запомнена, бюджет тратится впустую")


def test_a_server_failure_is_tried_again():
    """5xx — чужая поломка, к утру пройдёт: матч НЕ выбрасывается.

    Запомни мы его — одна ночь неполадок у OpenDota навсегда вычеркнула
    бы из выборки все матчи, которые в неё попали. Потеря была бы тихой:
    в логах ошибка одна и та же, а разницу видно только по тому, что
    матчи больше никогда не приходят.
    """
    src, looked_at = source_where({2: http_error(503)})
    drain(src, times=3)
    assert looked_at.count(2) == 3


def test_a_dropped_connection_is_tried_again():
    """Обрыв и таймаут — тем более временные: ответа не было вовсе."""
    src, looked_at = source_where({2: requests.ConnectionError("обрыв")})
    drain(src, times=3)
    assert looked_at.count(2) == 3


def test_an_expired_key_does_not_throw_away_the_listing():
    """403 НЕ отправляет матч в отказы — это про ключ, а не про матч.

    Соблазн велик: код постоянный, ответ не изменится. Но 403 одинаково
    относится КО ВСЕМ кандидатам, и запоминание по нему выкосило бы
    половину листинга за один цикл с протухшим ключом — а после замены
    ключа эти матчи уже не вернулись бы. Ошибка стоила бы куда дороже
    той, что чинится.
    """
    assert 403 not in PERMANENT_DETAIL_STATUSES
    src, looked_at = source_where({2: http_error(403)})
    drain(src, times=2)
    assert looked_at.count(2) == 2


def test_a_quota_refusal_still_stops_the_cycle():
    """429 по-прежнему обрывает цикл и в отказы не идёт.

    Остальные кандидаты дадут тот же ответ, поэтому продолжать — значит
    дожигать отрицательный остаток квоты. И матч тут ни при чём: вернуть
    его надо, когда квота восстановится.
    """
    src, looked_at = source_where({2: http_error(429)})
    for _ in range(2):
        src._pages.clear()          # как в drain: новый цикл — новый листинг
        with pytest.raises(requests.HTTPError):
            list(src.fetch_new(skip=lambda mid: False))
    assert looked_at.count(2) == 2, "429 запомнен как отказ — матч потерян"


def test_the_cycle_report_names_the_status_code(caplog):
    """Отчёт цикла называет КОД, а не только «ошибка запроса».

    Числом без кода нельзя решить, чинить что-то у себя или ждать чужого
    сервера, — а решения по постоянной и временной ошибке разные. Ровно
    так же устроена разбивка по причинам фильтра рядом.
    """
    src, _ = source_where({2: http_error(404), 1: http_error(503)})
    with caplog.at_level("INFO"):
        drain(src, times=1)
    line = "\n".join(r.getMessage() for r in caplog.records
                     if "цикл" in str(r.msg))
    assert "HTTP 404" in line and "HTTP 503" in line, line


def test_a_permanent_error_does_not_eat_the_inspection_budget_twice():
    """Освободившийся бюджет достаётся СЛЕДУЮЩИМ кандидатам.

    Это и есть смысл починки: не «меньше ошибок в логе», а больше
    осмотренных матчей за те же деньги. Во втором цикле вместо повторного
    404 источник обязан дойти до кандидата, до которого раньше не
    добирался.
    """
    src, looked_at = source_where({5: http_error(404)},
                                  candidates=(5, 4, 3, 2, 1))
    src._detail_budget = 2          # тесный бюджет: видно, кому он достался
    drain(src, times=2)
    assert looked_at[:2] == [5, 4], looked_at
    assert 5 not in looked_at[2:], "повторный осмотр съел бюджет второго цикла"
    assert looked_at[2:] == [4, 3], (
        f"после отказа бюджет не перешёл дальше по листингу: {looked_at}")
