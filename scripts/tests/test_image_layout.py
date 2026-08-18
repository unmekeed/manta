"""Код не должен предполагать раскладку монорепо (спринт 144).

ЧТО СЛУЧИЛОСЬ. Четыре модуля искали справочники из `libs/data`, отсчитывая
каталоги вверх от собственного файла:

    Path(__file__).resolve().parents[4] / "libs" / "data"

Четыре шага — это дорога от `apps/<svc>/src/<pkg>/x.py` до корня монорепо.
В образе тот же файл лежит по `/app/src/<pkg>/x.py`, и четвёртого шага там
нет: `Path.parents[4]` бросает IndexError. На VPS это уронило семь
коллекторов и report-generator в цикл перезапусков (2026-08-18).

ПОЧЕМУ ЭТО НЕ ЛОВИЛОСЬ. Дома сервисы поднимаются процессами из монорепо
(dev-recover.sh), где раскладка ровно та, под которую подогнано число.
Тесты — оттуда же. То есть предположение проверялось в единственном
окружении, где оно верно.

Хуже того, две из четырёх поломок были НЕвидимы и в образе:

  • timings.py держал выражение внутри `except Exception`, написанного на
    случай «файла нет». IndexError туда же и попадал: сервис поднимался
    успешно, со справочником предметов, молча оставшимся пустым.

  • builder.py перебирал кандидатов — сначала путь монорепо, потом путь
    образа, — и ловил в цикле в том числе IndexError. Но исключение
    бросала СБОРКА СПИСКА, до первой попытки. Обе раскладки были
    предусмотрены явно и описаны комментарием; работала одна.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. Не «есть ли в коде подозрительная строка», а
арифметика: для каждого `parents[N]`, посчитанного от `__file__`, индекс
обязан быть допустим В РАСКЛАДКЕ ОБРАЗА — то есть на пути, который файл
получит после COPY. Раскладка берётся из самого Dockerfile (WORKDIR +
COPY), а не записана здесь константой: перепишут Dockerfile — проверка
пойдёт за ним, а не начнёт врать.

Проверка статическая и потому не требует ни docker, ни установленных
зависимостей. Это важно: сторож упаковки из спринта 139 обходил
выполнение модулей СОЗНАТЕЛЬНО (`find_spec`, «иначе тест потребовал бы
grpc»), и ровно поэтому пропустил и grpcio, и вот это.
"""
import ast
import re
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT / "apps"


# -- раскладка образа из Dockerfile -----------------------------------------

def image_layout(app: str) -> tuple[PurePosixPath, list[tuple[str, str]]]:
    """(WORKDIR, [(что копируем, куда]) — как их объявляет Dockerfile.

    Числа тут не выдумываются: если завтра WORKDIR станет /srv, а исходники
    поедут в app/, проверка последует за файлом. Записанная константой, она
    начала бы утверждать неправду именно тогда, когда раскладка меняется, —
    то есть когда предположения в коде и ломаются.
    """
    text = (APPS / app / "Dockerfile").read_text(encoding="utf-8")
    workdir = PurePosixPath("/")
    copies: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if m := re.match(r"WORKDIR\s+(\S+)", line):
            workdir = PurePosixPath(m.group(1))
        elif m := re.match(r"COPY\s+(?!--)(\S+)\s+(\S+)\s*$", line):
            copies.append((m.group(1), m.group(2)))
    return workdir, copies


def image_path(app: str, repo_file: Path) -> PurePosixPath | None:
    """Где файл окажется в образе, или None — если он туда не копируется."""
    workdir, copies = image_layout(app)
    rel = repo_file.relative_to(ROOT).as_posix()
    for src, dst in copies:
        src_dir = src.rstrip("/")
        if rel == src_dir or rel.startswith(src_dir + "/"):
            tail = rel[len(src_dir):].lstrip("/")
            base = PurePosixPath(dst)
            if not base.is_absolute():
                base = workdir / base
            return base / tail if tail else base
    return None


# -- разбор parents[N] ------------------------------------------------------

def parents_indices(source: str) -> list[tuple[int, int]]:
    """[(индекс, строка)] для каждого `…parents[N]`, считанного от __file__.

    Учитывается и косвенная запись — `here = Path(__file__).resolve()`,
    затем `here.parents[4]`: именно так была написана одна из четырёх
    поломок, и проверка «в строке есть __file__» её бы не заметила.
    """
    tree = ast.parse(source)
    rooted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and "__file__" in ast.unparse(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    rooted.add(target.id)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        base = node.value
        if not (isinstance(base, ast.Attribute) and base.attr == "parents"):
            continue
        if not (isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)):
            continue  # срез или переменная — арифметику не восстановить
        src = ast.unparse(base.value)
        if "__file__" in src or src in rooted:
            found.append((node.slice.value, node.lineno))
    return found


def max_index(path: PurePosixPath) -> int:
    """Наибольший допустимый N для `Path(этот файл).parents[N]`."""
    return len(PurePosixPath(path).parents) - 1


# -- сам сторож -------------------------------------------------------------

def containerized_apps() -> list[str]:
    """Сервисы с образом и питоновскими исходниками.

    Фильтр именно «есть .py», а не «есть скопированные .py»: второй
    вариант выбрасывал бы из проверки ровно тот сервис, у которого COPY
    сломан, — то есть молчал бы в единственном случае, ради которого
    написан. frontend (TS) и replay-parser (C++) отсеиваются здесь честно:
    у них питоновских модулей нет вовсе.
    """
    return sorted(d.name for d in APPS.iterdir() if (d / "Dockerfile").exists()
                  and (d / "src").is_dir()
                  and any((d / "src").rglob("*.py")))


@pytest.mark.parametrize("app", containerized_apps())
def test_parents_index_is_valid_in_the_image_layout(app):
    """Каждый `parents[N]` обязан существовать и в образе, а не только дома.

    Это и есть та самая проверка ЭФФЕКТА: в монорепо все четыре поломки
    вычислялись без ошибки, потому что там глубина позволяет. Считать надо
    по пути, который файл получит ПОСЛЕ COPY.
    """
    bad = []
    for py in sorted((APPS / app / "src").rglob("*.py")):
        dst = image_path(app, py)
        if dst is None:
            continue
        limit = max_index(dst)
        for idx, line in parents_indices(py.read_text(encoding="utf-8")):
            if idx > limit:
                bad.append(
                    f"{py.relative_to(ROOT)}:{line}: parents[{idx}] — в образе "
                    f"файл лежит по {dst}, там доступно только parents[{limit}]")
    assert not bad, (
        f"{app}: путь считается по раскладке монорепо и в образе бросит "
        "IndexError:\n  " + "\n  ".join(bad))


@pytest.mark.parametrize("app", containerized_apps())
def test_sources_land_where_the_dockerfile_says(app):
    """Страховка от проверки пустоты.

    Если разбор Dockerfile однажды перестанет узнавать COPY, image_path
    начнёт возвращать None для всех файлов, и тест выше станет зелёным,
    не проверив ничего. Проверка ВЫШЕ молчит по той же причине, по какой
    молчал сторож упаковки: считает, что смотреть не на что.
    """
    copied = [p for p in (APPS / app / "src").rglob("*.py")
              if image_path(app, p) is not None]
    assert copied, f"{app}: ни один модуль из src/ не опознан как копируемый"


def test_analyzer_finds_the_original_defect():
    """Разбор ловит обе формы записи — прямую и через переменную.

    Мутация «сломать analyzer» иначе оставляет сторож зелёным навсегда:
    он ищет, ничего не находит и объявляет код чистым.
    """
    direct = "_DATA = Path(__file__).resolve().parents[4] / 'libs'"
    assert parents_indices(direct) == [(4, 1)]

    indirect = "here = Path(__file__).resolve()\nx = here.parents[4] / 'libs'"
    assert parents_indices(indirect) == [(4, 2)]

    unrelated = "p = some_other.parents[9]"
    assert parents_indices(unrelated) == []


def test_image_path_maps_a_known_file():
    """Отображение репозиторий → образ считается верно на живом примере."""
    got = image_path("data-collector",
                     APPS / "data-collector" / "src" / "collector" / "signals.py")
    assert got == PurePosixPath("/app/src/collector/signals.py")
    # Именно на этом пути parents[4] и падал: доступно 0..3.
    assert max_index(got) == 3


def test_shared_data_is_resolved_relative_to_libs():
    """Справочники libs/data ищутся от самого libs, а не от вызывающего.

    Это и есть замена подсчёту шагов: `libs/data` — сосед модулей libs в
    ЛЮБОЙ раскладке, потому что образы копируют `libs/` целиком.
    """
    import sys
    sys.path.insert(0, str(ROOT / "libs"))
    try:
        import manta_data
    finally:
        sys.path.pop(0)
    assert manta_data.DATA_DIR == ROOT / "libs" / "data"
    assert manta_data.data_path("heroes.json").exists()
    assert manta_data.load_json("heroes.json", {}), "справочник героев пуст"
    assert manta_data.load_json("нет-такого.json", {"по-умолчанию": 1}) == \
        {"по-умолчанию": 1}


def test_shared_data_survives_a_foreign_layout(tmp_path):
    """Тот же резолвер в ЧУЖОЙ раскладке — то есть проверка эффекта.

    Проверка выше запускается из монорепо, где верно было и старое
    вычисление: все четыре поломки считались там без ошибки. Настоящий
    вопрос — что будет, когда libs/ лежит на другой глубине. Здесь
    раскладка образа воспроизводится буквально: libs/ копируется в
    /<tmp>/app/libs, модуль импортируется оттуда, и путь к данным обязан
    сойтись — при том что до «корня монорепо» отсюда шагов не осталось.
    """
    import shutil
    import subprocess

    app = tmp_path / "app"
    shutil.copytree(ROOT / "libs", app / "libs")
    (app / "src" / "pkg").mkdir(parents=True)
    (app / "src" / "pkg" / "user.py").write_text(
        "from manta_data import DATA_DIR, load_json\n"
        "print(DATA_DIR)\n"
        "print(len(load_json('heroes.json', {})))\n", encoding="utf-8")

    proc = subprocess.run(
        [sys_executable(), str(app / "src" / "pkg" / "user.py")],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin",
             "PYTHONPATH": f"{app / 'src'}:{app / 'libs'}",
             "PYTHONDONTWRITEBYTECODE": "1"})
    assert proc.returncode == 0, f"импорт в раскладке образа упал:\n{proc.stderr}"
    where, count = proc.stdout.split()
    assert where == str(app / "libs" / "data")
    assert int(count) > 0, "справочник героев прочитался пустым"


def sys_executable() -> str:
    import sys
    return sys.executable


def test_no_module_computes_the_libs_data_path_itself():
    """Правило, а не случай: путь к libs/data не собирают по месту.

    Четыре модуля делали это четырьмя разными числами шагов, и каждый был
    прав в своей раскладке. Проверка индексов выше поймала бы новый
    промах только при неверном N; эта запрещает саму привычку, из-за
    которой промах становится возможен.
    """
    pattern = re.compile(r'parents\[\d+\][^\n]*["\']libs["\']')
    bad = []
    for py in sorted(APPS.glob("*/src/**/*.py")):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # объяснение, почему так больше не делают
            if pattern.search(line):
                bad.append(f"{py.relative_to(ROOT)}:{i}")
    assert not bad, (
        "путь к libs/ считается шагами вверх — используйте manta_data:\n  "
        + "\n  ".join(bad))
