"""Список публичных маршрутов один, а записан трижды (спринт 205).

ГДЕ ОН ЖИВЁТ:

  * `PublicRoutes` в `api-gateway/internal/router/public.go` — то, что
    сервер РЕАЛЬНО отдаёт наружу;
  * регулярки в `apps/manta-site/app/api/manta/[...path]/route.ts` — то,
    что серверный прокси фронта пропускает к API;
  * `openapi/manta.yaml` — то, по чему пишут клиента.

Разъезд между ними МОЛЧАЛИВ и проявляется по-разному. Маршрут, забытый в
прокси, отобьётся 404 — и искать будут в API, где он есть. Маршрут,
забытый в спецификации, просто не найдёт тот, кто пишет фронт. А маршрут,
описанный в спецификации, но не существующий в коде, хуже всех: по нему
пишут клиента, и обнаруживается это на живом запросе.

ЭТО НЕ УМОЗРИТЕЛЬНО. На момент написания теста спецификация обещала
`GET /api/v1/meta/heroes`, а сервер отдавал `GET /api/v1/heroes`: путь из
документа возвращал 404, и заметить это можно было, только сходив по
нему.

ПОЧЕМУ СРАВНЕНИЕМ, А НЕ ГЕНЕРАЦИЕЙ. Породить прокси из Go нечем: сборка
фронта и сборка шлюза не встречаются. Сравнение слабее генерации, зато
ловит разъезд с ЛЮБОЙ стороны, включая правку в спецификации, из которой
ничего не порождается.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора OpenAPI")

ROOT = Path(__file__).resolve().parents[2]
GO = ROOT / "apps" / "api-gateway" / "internal" / "router" / "public.go"
PROXY = ROOT / "apps" / "manta-site" / "app" / "api" / "manta" / "[...path]" / "route.ts"
SPEC = ROOT / "openapi" / "manta.yaml"

PREFIX = "/api/v1/"


def go_routes() -> set[str]:
    """Что сервер отдаёт наружу: ключи карты PublicRoutes.

    Берётся из ТОЙ ЖЕ карты, по которой идёт регистрация: список «для
    теста» рядом с регистрацией проверял бы список, а ходили бы в
    регистрацию.
    """
    body = GO.read_text(encoding="utf-8").split("func PublicRoutes", 1)[1]
    body = body.split("\n}", 1)[0]
    return {f"{m} {p}" for m, p in
            re.findall(r'"(GET|POST) (/api/v1/[^"]+)":', body)}


def proxy_routes() -> set[str]:
    """Что пропускает прокси фронта.

    Регулярки живут без префикса и с `\\d+` вместо имени параметра;
    приводим к виду Go-паттерна, чтобы сравнивать одно с одним. Метод в
    прокси решается отдельной строкой (`draft` только POST), поэтому он
    выводится оттуда же.
    """
    src = PROXY.read_text(encoding="utf-8")
    line = re.search(r"const routes = \[(.*?)\];", src, re.S)
    assert line, "в прокси не найден список маршрутов"
    post_only = re.search(r'const draft = path === "([^"]+)"', src)
    post_path = post_only.group(1) if post_only else ""

    out = set()
    for rx in re.findall(r"/\^(.+?)\$/", line.group(1)):
        path = rx.replace("\\/", "/").replace("\\d+", "{id}")
        method = "POST" if path == post_path else "GET"
        out.add(f"{method} {PREFIX}{path}")
    return out


def spec_routes(include_planned: bool = False) -> set[str]:
    """Что описано в спецификации (все маршруты, не только публичные).

    Маршрут с `x-manta-status: planned` — ЗАМЫСЕЛ, а не обещание: он
    описан, но шлюзом не отдаётся. Различать их обязательно: без пометки
    документ обещает маршрут, по которому напишут клиента и получат 404,
    а вычеркнуть проектную работу ради зелёного теста значило бы потерять
    её вовсе.
    """
    doc = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    out = set()
    for p, ops in (doc.get("paths") or {}).items():
        if not include_planned and ops.get("x-manta-status") == "planned":
            continue
        out |= {f"{m.upper()} {p}" for m in ops
                if m in ("get", "post", "put", "delete")}
    return out


def planned_routes() -> set[str]:
    return spec_routes(include_planned=True) - spec_routes()


def normalise(routes: set[str]) -> set[str]:
    """Имя параметра пути значения не имеет, форма — имеет.

    Go пишет `{matchId}`, спецификация — `{matchId}`, прокси знает лишь
    «здесь число». Сравнивать имена значило бы ловить переименование
    переменной вместо расхождения маршрутов.
    """
    return {re.sub(r"\{[^}]+\}", "{id}", r) for r in routes}


def test_the_parsing_finds_all_three_lists():
    """Страховка от проверки пустоты.

    Сломайся любой разбор — сравнение ниже прошло бы на пустых
    множествах. Этот проект уже ловил такое на себе не раз.
    """
    assert len(go_routes()) >= 5, go_routes()
    assert len(proxy_routes()) >= 5, proxy_routes()
    assert len(spec_routes()) >= 5, spec_routes()


def test_the_proxy_passes_exactly_the_public_routes():
    """ГЛАВНОЕ: прокси фронта пропускает ровно то, что сервер отдаёт.

    Лишний маршрут в прокси — обещание, которое кончится 404 от API.
    Недостающий — работающий маршрут, который прокси отобьёт сам, и
    искать причину будут в API, где всё в порядке.
    """
    assert normalise(proxy_routes()) == normalise(go_routes()), (
        f"прокси: {sorted(normalise(proxy_routes()))}\n"
        f"сервер: {sorted(normalise(go_routes()))}")


def test_every_public_route_is_documented():
    """Каждый публичный маршрут описан в спецификации.

    По ней пишут клиента. Недокументированный маршрут существует только
    для того, кто читал исходники шлюза.
    """
    missing = normalise(go_routes()) - normalise(spec_routes())
    assert not missing, (
        f"публичные маршруты вне спецификации: {sorted(missing)}")


def test_the_spec_does_not_promise_routes_that_do_not_exist():
    """Спецификация не обещает того, чего сервер не отдаёт.

    Худший из трёх видов разъезда: по документу пишут клиента, и
    обнаруживается всё на живом запросе. Ровно это и было найдено при
    написании теста — спецификация обещала `/api/v1/meta/heroes`, сервер
    отдавал `/api/v1/heroes`.

    Сравнивается с ПОЛНЫМ набором маршрутов шлюза (публичные плюс
    закрытые), а не только с публичными: приватные эндпоинты в
    спецификации описаны законно, наружу они просто не смотрят.
    """
    router = ROOT / "apps" / "api-gateway" / "internal" / "router" / "router.go"
    src = router.read_text(encoding="utf-8")
    served = {f"{m} {p}" for m, p in
              re.findall(r'"(GET|POST|DELETE|PUT) (/api/v1/[^"]+)"', src)}
    served |= go_routes()

    ghosts = normalise(spec_routes()) - normalise(served)
    assert not ghosts, (
        f"спецификация обещает несуществующие маршруты: {sorted(ghosts)}. "
        f"По ним напишут клиента, и узнают об этом на живом запросе. "
        f"Если маршрут ЗАПЛАНИРОВАН, а не забыт — пометить его в "
        f"openapi/manta.yaml как `x-manta-status: planned` С ПРИЧИНОЙ")


def test_a_planned_route_is_not_served_yet():
    """Помеченное «запланировано» и правда не отдаётся.

    Пометка снимает маршрут с проверки, и потому сама может стать
    лазейкой: ею легко заглушить настоящий разъезд. Поэтому смысл
    проверяется в обратную сторону — запланированного в коде быть не
    должно, а появившись, оно обязано потерять пометку.
    """
    router = ROOT / "apps" / "api-gateway" / "internal" / "router" / "router.go"
    src = router.read_text(encoding="utf-8")
    served = {f"{m} {p}" for m, p in
              re.findall(r'"(GET|POST|DELETE|PUT) (/api/v1/[^"]+)"', src)}
    served |= go_routes()

    late = normalise(planned_routes()) & normalise(served)
    assert not late, (
        f"маршруты помечены запланированными, но уже отдаются: "
        f"{sorted(late)} — пометку пора снять, иначе она прикроет "
        f"настоящий разъезд")
