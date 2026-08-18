"""Сборка образа не должна искать за границей своего контекста (139, 143).

Спринт 139 закрыл эту дыру у питоновских сервисов, спринт 143 — у Go.
Второй случай доказал, что сторож был написан слишком узко: он знал про
`libs/` и про Python, поэтому api-gateway, который через `replace` в
go.mod тянет `../../proto/gen/go`, прошёл мимо него и упал только на
живой VPS.

Общее правило у обоих случаев одно: КАЖДЫЙ путь, нужный сборке, обязан
лежать внутри объявленного контекста. COPY не умеет выходить за него, и
нарушение вскрывается не при сборке образа на машине разработчика (там
всё собирается из исходников на хосте), а при первом развёртывании.

ЧТО СЛУЧИЛОСЬ. Спринт 139 свёл координаты карты в libs/dota_map.py, и
data-collector с feature-extractor начали импортировать оттуда. Тогда и
выяснилось, что образы этих сервисов libs/ не содержат ВООБЩЕ: контекст
сборки был `../apps/<сервис>`, а COPY не умеет выходить за контекст.

И это не новая поломка. `import manta_grpc` стоял в точках входа всех
питоновских сервисов задолго до, а ml-service и report-generator ещё и
импортируют wp_rates. То есть весь профиль `apps` в docker-compose падал
при старте на ImportError — и падал МОЛЧА, по трём причинам сразу:

  1. Образ СОБИРАЛСЯ успешно. Ошибка не в сборке, а в запуске.
  2. `restart: unless-stopped` превращал креш-цикл в ровный шум в логах.
  3. Разработка идёт мимо Docker: dev-recover.sh поднимает те же сервисы
     процессами на хосте с PYTHONPATH=src:$ROOT/libs, и там всё работает.

Отдельно коварен был report-generator: libs/ в образ копировался (ради
data/heroes.json), но в PYTHONPATH не попадал. Каталог в образе ЕСТЬ,
глазами проверка проходит, а импорт по имени не работает.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. Список сервисов не записан в тесте — он ВЫВОДИТСЯ
из исходников. Записанный руками, он разошёлся бы с кодом ровно тогда,
когда это дороже всего: при добавлении нового импорта из libs в сервис,
который раньше без них обходился. Такой импорт добавляют, не думая о
Dockerfile, — как и случилось в этом спринте.
"""
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML нужен для разбора compose")

ROOT = Path(__file__).resolve().parents[2]
LIBS = ROOT / "libs"
APPS = ROOT / "apps"
COMPOSE = ROOT / "deployments" / "docker-compose.yml"
RECOVER = ROOT / "scripts" / "dev-recover.sh"


def libs_module_names() -> set[str]:
    """Что из libs/ вообще можно импортировать по голому имени."""
    names = set()
    for entry in LIBS.iterdir():
        if entry.name.startswith((".", "_")):
            continue
        if entry.suffix == ".py":
            names.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)
    return names


def apps_importing_libs() -> dict[str, set[str]]:
    """Сервис -> какие модули libs он импортирует по имени."""
    modules = libs_module_names()
    if not modules:
        pytest.fail("в libs/ не нашлось ни одного импортируемого модуля")
    alt = "|".join(sorted(re.escape(m) for m in modules))
    pattern = re.compile(rf"^\s*(?:from|import)\s+({alt})\b", re.M)

    found: dict[str, set[str]] = {}
    for src in sorted(APPS.glob("*/src")):
        app = src.parent.name
        for py in src.rglob("*.py"):
            for hit in pattern.findall(py.read_text(encoding="utf-8")):
                found.setdefault(app, set()).add(hit)
    return found


def containerized() -> list[str]:
    """Сервисы с образом: у них libs обязан лежать В ОБРАЗЕ."""
    return sorted(a for a in apps_importing_libs() if dockerfile_of(a).exists())


def host_only() -> list[str]:
    """Сервисы без образа: живут процессами, libs приходит из PYTHONPATH.

    Это не недоделка. coach, draft, similarity и feature-store поднимает
    dev-recover.sh, и до Docker они пока не дошли. Но гарантия им нужна
    та же самая — просто выдаётся в другом месте, и проверять её надо
    именно там.
    """
    return sorted(a for a in apps_importing_libs() if not dockerfile_of(a).exists())


def compose_services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8")).get("services", {})


def build_of(service: dict) -> tuple[str, str]:
    """(контекст, путь к Dockerfile) в том виде, как их видит compose."""
    build = service.get("build")
    if isinstance(build, str):
        return build, f"{build}/Dockerfile"
    if isinstance(build, dict):
        ctx = build.get("context", ".")
        return ctx, build.get("dockerfile", f"{ctx}/Dockerfile")
    return "", ""


def dockerfile_of(app: str) -> Path:
    return APPS / app / "Dockerfile"


# -- ради чего всё --------------------------------------------------------------

def test_some_app_actually_imports_libs():
    """Страховка от теста, который проверяет пустое множество.

    Если разбор исходников однажды сломается — опечатка в маске, съехавший
    путь, — все проверки ниже начнут проходить, ничего не проверяя. Это
    худший исход: зелёный тест вместо отсутствующего.
    """
    found = apps_importing_libs()
    assert found, "ни один сервис не импортирует libs — разбор сломан"
    assert "dota_map" in set().union(*found.values()), \
        "dota_map не найден среди импортов, хотя ради него спринт и затевался"
    assert containerized(), "ни одного сервиса с образом — разбор сломан"


@pytest.mark.parametrize("app", host_only())
def test_host_launcher_puts_libs_on_pythonpath(app):
    """Сервис без образа поднимается с libs в PYTHONPATH.

    Гарантия та же, что и у контейнерных, но выдаётся в другом месте.
    Если сервис поднимают с PYTHONPATH=src и без libs, он упадёт на
    старте — а dev-recover.sh запускает всё через nohup, и падение
    осядет в логе, а не на глазах.
    """
    text = RECOVER.read_text(encoding="utf-8")
    launches = [l for l in text.splitlines() if f"apps/{app} " in l or f"apps/{app}&&" in l]
    assert launches, f"{app}: dev-recover.sh его не поднимает — кто тогда?"
    for line in launches:
        assert "PYTHONPATH=src:$ROOT/libs" in line, (
            f"{app}: запуск без libs в PYTHONPATH — импорт упадёт:\n{line.strip()}")


@pytest.mark.parametrize("app", containerized())
def test_image_ships_libs(app):
    """Импортирует из libs — значит libs лежит в образе и виден Python.

    Две половины, и без любой из них образ падает при старте. У
    report-generator была ровно вторая: каталог скопирован, PYTHONPATH о
    нём не знает.
    """
    text = dockerfile_of(app).read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))

    copies = re.findall(r"^\s*COPY\s+libs/\s+(\S+)", body, re.M)
    assert copies, f"{app}: образ не копирует libs/, а импортирует из него"

    env = re.search(r"^\s*ENV\s+PYTHONPATH=(\S+)", body, re.M)
    assert env, f"{app}: в Dockerfile нет ENV PYTHONPATH"
    paths = env.group(1).split(":")
    dest = copies[0].rstrip("/")
    want = dest if dest.startswith("/") else f"/app/{dest}"
    assert want in paths, (
        f"{app}: libs копируется в {want}, но PYTHONPATH={env.group(1)} "
        f"его не содержит — импорт по имени не найдёт модуль")


@pytest.mark.parametrize("app", containerized())
def test_copy_paths_resolve_from_the_declared_context(app):
    """Каждый COPY существует ОТНОСИТЕЛЬНО объявленного контекста.

    Это и есть та проверка, которой не было. `COPY libs/` из контекста
    apps/<сервис> не собралось бы вовсе, а `COPY src/` из корня молча
    скопировал бы не тот каталог — если бы в корне оказался src/. Ошибка
    сборки заметна сразу, подмена каталога — нет.
    """
    services = compose_services()
    users = {n: s for n, s in services.items()
             if build_of(s)[1].replace("../", "").endswith(f"apps/{app}/Dockerfile")
             or build_of(s)[0] == f"../apps/{app}"}
    assert users, f"{app}: сервис не найден в docker-compose.yml"

    text = dockerfile_of(app).read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    sources = [m.group(1) for m in
               re.finditer(r"^\s*COPY\s+(?!--from)(\S+)", body, re.M)]
    assert sources, f"{app}: в Dockerfile нет ни одного COPY"

    for name, svc in users.items():
        ctx = (COMPOSE.parent / build_of(svc)[0]).resolve()
        for src in sources:
            assert (ctx / src).exists(), (
                f"{name}: COPY {src} не разрешается из контекста {ctx} — "
                f"сборка образа упадёт")


@pytest.mark.parametrize("app", containerized())
def test_imports_resolve_inside_the_image_layout(app, tmp_path):
    """Собрать раскладку образа НА ДИСКЕ и спросить у Python, найдётся ли модуль.

    Проверки выше читают Dockerfile глазами: есть ли COPY, упомянут ли
    путь в PYTHONPATH. Так можно проглядеть ровно то, на чём сломался
    report-generator: обе строки на месте, а импорт всё равно не
    работает, потому что каталоги и переменная говорят о разных путях.

    Здесь вместо чтения — воспроизведение. Каждый COPY переносится по
    своему адресу назначения, PYTHONPATH берётся из ENV как есть, и
    ответ даёт сам импортёр Python, а не наше мнение о нём.

    find_spec, а не import: он находит модуль, НЕ выполняя его. Иначе
    тест потребовал бы confluent_kafka, grpc и lightgbm и превратился бы
    из проверки упаковки в проверку зависимостей.
    """
    text = dockerfile_of(app).read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    ctx = ROOT

    # WORKDIR читается, а не подразумевается. Относительный `COPY libs/
    # libs/` кладёт каталог рядом с WORKDIR, а PYTHONPATH записан
    # абсолютным путём — если одно поменять, а второе забыть, каталог
    # окажется не там, где его ищут, и обе текстовые проверки выше этого
    # не увидят: каждая по отдельности выглядит правильной.
    wd = re.search(r"^\s*WORKDIR\s+(\S+)", body, re.M)
    assert wd, f"{app}: в Dockerfile нет WORKDIR"
    image = tmp_path / "image"
    workdir = image / wd.group(1).lstrip("/")
    workdir.mkdir(parents=True)

    for m in re.finditer(r"^\s*COPY\s+(?!--from)(\S+)\s+(\S+)", body, re.M):
        src, dst = ctx / m.group(1), m.group(2)
        if not src.is_dir():
            continue
        target = workdir / dst.lstrip("/") if not dst.startswith("/") \
            else image / dst.lstrip("/")
        shutil.copytree(src, target, dirs_exist_ok=True)

    env = re.search(r"^\s*ENV\s+PYTHONPATH=(\S+)", body, re.M)
    pypath = ":".join(str(image / p.lstrip("/")) for p in env.group(1).split(":"))

    wanted = sorted(apps_importing_libs()[app])
    probe = textwrap.dedent(f"""
        import importlib.util, sys
        missing = [m for m in {wanted!r} if importlib.util.find_spec(m) is None]
        sys.exit("не найдены в образе: " + ", ".join(missing) if missing else 0)
    """)
    proc = subprocess.run([sys.executable, "-c", probe],
                          env={"PYTHONPATH": pypath, "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, cwd=workdir)
    assert proc.returncode == 0, (
        f"{app}: в раскладке образа импорт не разрешается — "
        f"{proc.stderr.strip()}\nPYTHONPATH={env.group(1)}")


# -- то же правило для Go (спринт 143) --------------------------------------------

GO_MODULES = sorted(APPS.glob("*/go.mod")) + sorted(APPS.glob("*/*/go.mod"))


def go_services() -> list[Path]:
    """Каталоги Go-сервисов, у которых есть свой Dockerfile.

    go.mod может лежать глубже каталога сервиса (parser-svc держит его в
    svc/), поэтому Dockerfile ищется вверх по дереву до apps/.
    """
    out = []
    for mod in GO_MODULES:
        d = mod.parent
        while d != APPS and d.parent != d:
            if (d / "Dockerfile").exists():
                out.append(mod)
                break
            d = d.parent
    return out


def go_replace_paths(mod: Path) -> list[str]:
    """Относительные пути из директив replace."""
    text = mod.read_text(encoding="utf-8")
    return [m.group(1) for m in
            re.finditer(r"^replace\s+\S+\s+=>\s+(\.\.?/\S+)", text, re.M)]


def test_some_go_module_uses_replace():
    """Страховка от проверки пустого множества.

    Если разбор go.mod однажды сломается, проверка ниже начнёт проходить,
    ничего не проверяя, — а именно она ловит поломку, стоившую живого
    развёртывания.
    """
    assert GO_MODULES, "не найдено ни одного go.mod — разбор сломан"
    assert any(go_replace_paths(m) for m in GO_MODULES), \
        "ни одного replace — разбор сломан или директива исчезла"


@pytest.mark.parametrize("mod", go_services(), ids=lambda m: m.parent.name)
def test_go_replace_targets_are_inside_the_build_context(mod):
    """Путь из replace обязан лежать ВНУТРИ контекста сборки.

    Именно это и упало на VPS: go.mod api-gateway ссылается на
    ../../proto/gen/go, контекст был apps/api-gateway, и `go mod
    download` не нашёл /proto/gen/go/go.mod. На домашней машине это не
    всплывало — там `go build` идёт на хосте, где путь существует.
    """
    replaces = go_replace_paths(mod)
    if not replaces:
        pytest.skip("модуль без replace — выходить за контекст незачем")

    # Ищем сервис в compose так же, как для питоновских: по Dockerfile.
    d = mod.parent
    while not (d / "Dockerfile").exists():
        d = d.parent
    dockerfile = (d / "Dockerfile").relative_to(ROOT)

    services = compose_services()
    users = {n: s for n, s in services.items()
             if build_of(s)[1].replace("../", "").endswith(str(dockerfile))
             or build_of(s)[0] == f"../{d.relative_to(ROOT)}"}
    assert users, f"{d.name}: сервис не найден в docker-compose.yml"

    for name, svc in users.items():
        ctx = (COMPOSE.parent / build_of(svc)[0]).resolve()
        for rel in replaces:
            target = (mod.parent / rel).resolve()
            assert target.exists(), f"{name}: путь {rel} не существует вовсе"
            assert target.is_relative_to(ctx), (
                f"{name}: replace ведёт в {target}, а контекст сборки {ctx} — "
                f"COPY туда не дотянется, и `go mod download` упадёт")


@pytest.mark.parametrize("mod", go_services(), ids=lambda m: m.parent.name)
def test_go_image_copies_every_replace_target(mod):
    """Мало быть внутри контекста — путь должен ещё и КОПИРОВАТЬСЯ в образ.

    Контекст можно расширить до корня репозитория и на этом успокоиться:
    сборка тогда падает не на «нет каталога», а позже и невнятнее.
    """
    replaces = go_replace_paths(mod)
    if not replaces:
        pytest.skip("модуль без replace")
    d = mod.parent
    while not (d / "Dockerfile").exists():
        d = d.parent
    body = "\n".join(l for l in (d / "Dockerfile").read_text(encoding="utf-8")
                     .splitlines() if not l.lstrip().startswith("#"))
    copies = [m.group(1) for m in
              re.finditer(r"^\s*COPY\s+(?!--from)(\S+)", body, re.M)]
    for rel in replaces:
        target = (mod.parent / rel).resolve().relative_to(ROOT)
        assert any(str(target).startswith(c.rstrip("/")) or
                   c.rstrip("/").startswith(str(target)) for c in copies), \
            f"{d.name}: {target} не копируется в образ (COPY: {copies})"
