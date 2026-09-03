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
import ast
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


# -- зависимости самих модулей libs (спринт 143) ----------------------------------

# Имя импорта не всегда равно имени пакета. Словарь маленький и ведётся
# руками СОЗНАТЕЛЬНО: вывести соответствие неоткуда, зато забытая пара
# ловится сразу — тест не найдёт пакет в requirements и упадёт.
IMPORT_TO_PACKAGE: dict[str, tuple[str, ...]] = {
    "grpc": ("grpcio",),
    "yaml": ("PyYAML",),
    "sklearn": ("scikit-learn",),
    # `from google.protobuf import ...` в сгенерированных стабах: пакет
    # занимает namespace-каталог google/, а называется protobuf.
    "google": ("protobuf",),
    # Урезанный клиент даёт тот же модуль mlflow, что и полный; годится
    # любой из двух. Перечислены оба, а не «любой, чьё имя начинается
    # с mlflow»: второе пропустило бы опечатку.
    "mlflow": ("mlflow-skinny", "mlflow"),
}


def acceptable_packages(imp: str) -> set[str]:
    """Каким пакетом может быть удовлетворён импорт (нормализованно)."""
    return {norm(p) for p in IMPORT_TO_PACKAGE.get(imp, (imp,))}


# Стандартная библиотека берётся У ИНТЕРПРЕТАТОРА, а не выписывается
# руками (спринт 191). Здесь лежал перечень из двадцати имён, и первый же
# новый модуль libs, импортировавший `html`, объявил стандартный модуль
# недостающим ПАКЕТОМ: тест потребовал добавить в requirements.txt то,
# чего в PyPI нет. Список, который надо не забыть пополнить, — дефект той
# же формы, что комментарий «дописывать сюда»: работает ровно до первого
# случая, о котором не подумали.
#
# Ниже по файлу `sys.stdlib_module_names` уже используется для той же
# цели. Две версии одного понятия в одном файле разошлись, как и
# положено, молча.
STDLIB_HINT = set(sys.stdlib_module_names) | {"__future__"}


def libs_module_imports(module: str) -> set[str]:
    """Сторонние импорты модуля libs (по имени импорта, не пакета)."""
    path = LIBS / f"{module}.py"
    if not path.exists():
        path = LIBS / module / "__init__.py"
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    names = set(re.findall(r"^\s*import\s+([a-zA-Z_][\w]*)", text, re.M))
    names |= set(re.findall(r"^\s*from\s+([a-zA-Z_][\w]*)", text, re.M))
    return {n for n in names if n not in STDLIB_HINT and not n.startswith("_")}


def norm(name: str) -> str:
    """Имя пакета в сравнимом виде.

    На PyPI пишут через дефис (prometheus-client), импортируют через
    подчёркивание (prometheus_client). Сравнение «в лоб» объявляло бы
    отсутствующим уже объявленный пакет — ложная тревога на верной
    конфигурации.
    """
    return name.lower().replace("_", "-")


def parse_requirements(text: str) -> set[str]:
    """Что реально ставится по такому requirements.txt.

    Имя выделяется РАЗБОРОМ строки, а не поиском подстроки. Подстрока
    считала бы объявленным пакет, чьё имя лишь входит в чужое, и —
    что случалось трижды за развёртывание — пакет, упомянутый в
    комментарии. Комментарий тут отрезается первым делом.

    Разбор вынесен из чтения файла НАРОЧНО: пока он умел только читать
    ml-service, проверить его можно было лишь на живом файле, а там нет
    ни закомментированного пакета, ни имени-подстроки. Мутация «считать
    комментарии за строки» такую проверку пережила.
    """
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0]
        if name:
            out.add(norm(name))
    return out


def declared_packages(app: str) -> set[str]:
    """Что ставится из requirements.txt сервиса."""
    path = APPS / app / "requirements.txt"
    return parse_requirements(path.read_text(encoding="utf-8")) if path.exists() else set()


def test_parse_requirements_ignores_what_is_not_installed():
    """Опаснее всего разбор, который возвращает ЛИШНЕЕ.

    Недостающая зависимость тогда снова пройдёт: сторож увидит нужное
    имя там, где стоит лишь упоминание о нём. Все три случая — из
    жизни этого развёртывания.
    """
    got = parse_requirements(
        "minio>=7.2\n"
        "# grpcio>=1.60 — поставим потом\n"      # закомментировано = не стоит
        "prometheus-client>=0.20  # метрики\n"   # хвостовой комментарий
        "-r base.txt\n"                          # не пакет
        "uvicorn[standard]==0.30\n"
        "\n")
    assert got == {"minio", "prometheus-client", "uvicorn"}
    assert "grpcio" not in got, "закомментированный пакет считается стоящим"
    assert "base.txt" not in got and "метрики" not in got


def test_declared_packages_reads_a_known_file():
    """Страховка от разбора, который перестал что-либо находить."""
    pkgs = declared_packages("ml-service")
    assert {"minio", "scikit-learn", "protobuf", "optuna"} <= pkgs


def test_libs_modules_have_third_party_imports():
    """Страховка от проверки пустого множества."""
    assert "grpc" in libs_module_imports("manta_grpc"), \
        "разбор импортов libs сломан"


def app_source_imports(app: str) -> dict[str, str]:
    """{имя импорта: где впервые встречен} — сторонние импорты самого сервиса.

    Учитываются импорты ЛЮБОЙ глубины, включая те, что стоят внутри
    функций. Именно так был спрятан `minio`: `from minio import Minio`
    внутри `MinioBackend.__init__`, с пометкой «импорт по месту: тесты
    живут на фейке». Из-за неё зависимость не попалась на глаза при сборке
    образа, а реестр моделей на MinIO — путь ПО УМОЛЧАНИЮ: ml-service на
    VPS падал на первом же resolve().
    """
    src = APPS / app / "src"
    local = {p.stem for p in src.rglob("*.py")}
    local |= {d.name for d in src.rglob("*") if d.is_dir()}
    stdlib = set(sys.stdlib_module_names)

    found: dict[str, str] = {}
    for py in sorted(src.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        for n in names - stdlib - local - libs_module_names():
            found.setdefault(n, str(py.relative_to(ROOT)))
    return found


def test_app_source_imports_finds_the_hidden_one():
    """Разбор видит импорт, спрятанный внутри функции.

    Без этого проверка ниже осталась бы зелёной при сломанном разборе:
    ничего не нашла — значит, всё объявлено.
    """
    found = app_source_imports("ml-service")
    assert "minio" in found, "импорт внутри функции не найден"
    assert "lightgbm" in found
    assert "registry" not in found, "свой же модуль принят за сторонний"
    assert "json" not in found, "стандартная библиотека принята за стороннюю"


@pytest.mark.parametrize("app", containerized())
def test_image_installs_everything_the_service_imports(app):
    """Всё, что сервис импортирует, объявлено в его requirements.

    Правило без исключений — и это осознанный выбор. Рассуждение «этот
    импорт по-настоящему не нужен, путь ведь не запускается» уже дало два
    креш-цикла подряд: grpcio (спринт 143) и minio с optuna (144). Оба
    раза дома всё работало, потому что сервисы живут процессами в общем
    окружении, где нужный пакет уже стоит от соседа.
    """
    if not (APPS / app / "requirements.txt").exists():
        pytest.skip(f"{app}: нет requirements.txt")
    declared = declared_packages(app)
    missing = [f"{imp} (пакет {'/'.join(sorted(acceptable_packages(imp)))}) — {where}"
               for imp, where in sorted(app_source_imports(app).items())
               if not acceptable_packages(imp) & declared]
    assert not missing, (
        f"{app}: импортирует, но не устанавливает:\n  " + "\n  ".join(missing))


@pytest.mark.parametrize("app", containerized())
def test_image_installs_dependencies_of_the_libs_it_imports(app):
    """Мало положить libs в образ — нужны и ЗАВИСИМОСТИ этих модулей.

    Спринт 139 починил «libs нет в образе» и на этом остановился. Файл
    оказался на месте, а импортировать его было нечем: libs/manta_grpc
    тянет grpc, и в requirements коллектора grpcio не было. Все семь
    коллекторов падали на старте и перезапускались по кругу.

    Проверка упаковки, написанная тогда, обходила это СОЗНАТЕЛЬНО: она
    искала модуль через find_spec, не выполняя его, «чтобы не требовать
    grpc и lightgbm». То есть ровно та зависимость, что сломалась, была
    вынесена за скобки — и запись об этом стоит в комментарии к тесту.
    """
    if not (APPS / app / "requirements.txt").exists():
        pytest.skip(f"{app}: нет requirements.txt")
    declared = declared_packages(app)

    missing = []
    for module in sorted(apps_importing_libs()[app]):
        for imp in sorted(libs_module_imports(module)):
            if not acceptable_packages(imp) & declared:
                missing.append(
                    f"{module} → import {imp} → пакет "
                    f"{'/'.join(sorted(acceptable_packages(imp)))}")
    assert not missing, (
        f"{app}: импортирует модули libs, но их зависимостей нет в "
        f"requirements.txt:\n  " + "\n  ".join(missing))
