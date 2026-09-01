"""doctor обязан говорить правду на ОБЕИХ машинах (спринт 163).

ЧТО СЛУЧИЛОСЬ. На VPS `make doctor` докладывал две беды, и обе были
выдуманы:

    WARN 11 процесс(ов) не запущено   — при двадцати живых контейнерах
    FAIL журнал SchemaMigrations пуст — при полностью применённых миграциях

Первая: проверка искала сервисы через `pgrep`, как дома, а на VPS они
живут в Docker. Вторая: журнал спрашивался хостовым `psql`, которого на
VPS нет и быть не должно (спринты 143 и 152 — инструмент берём оттуда,
где он уже есть), а `2>/dev/null` превращал «клиента нет» в «журнал
пуст». В двадцати строках ниже проверка ClickHouse всё это время ходила
правильно, через контейнер: верное решение применили к одной базе и не
применили к соседней.

ЧЕМ ЭТО ПЛОХО. `heartbeat.sh` считает вердикт по `doctor` и шлёт его в
Telegram ежедневно. Значит здоровая машина каждое утро присылала бы
🔴 — ложную тревогу по расписанию. Через неделю такой канал перестают
читать, и следом мимо проходит настоящая беда. В этом проекте так уже
было: тринадцать дней никто не замечал, что бэкап не снимается.

КАК ПРОВЕРЯЕТСЯ. Скрипт гоняется целиком со стабами `docker`, `pgrep`,
`curl` и `psql` — то есть проверяется настоящий код, а не пересказ.
Утверждения только про два раздела; остальной вердикт на стабах смысла
не имеет.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "scripts" / "doctor.sh"

# Сервисы, которые doctor обязан считать живыми. Список короткий
# намеренно: если проверка перестанет их видеть, беда будет ровно та же,
# что и была.
SERVICES = ["data-collector", "feature-extractor", "ml-service",
            "report-generator", "auto-train", "parser-svc"]


def run_doctor(tmp_path, *, containers_running: bool, processes_running: bool,
               pg_in_container: bool = True, migrations: str | None = None):
    """Прогнать doctor.sh в выдуманном мире.

    containers_running — топология VPS (сервисы в Docker);
    processes_running  — топология дома (сервисы процессами);
    pg_in_container    — отвечает ли `docker exec ... psql`;
    migrations         — что возвращает журнал (None = пусто).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    if migrations is None:
        migrations = ""
    else:
        migrations = "\n".join(sorted(
            p.name for p in (ROOT / "infra" / "migrations" / "postgres").glob("*.sql")))

    # docker: inspect отвечает про контейнеры, exec — про psql внутри.
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  inspect)\n'
        f'    {"echo true" if containers_running else "exit 1"}\n'
        '    ;;\n'
        '  exec)\n'
        f'    {"" if pg_in_container else "exit 1"}\n'
        '    # SELECT 1 — проба доступности; остальное — журнал миграций.\n'
        '    case "$*" in\n'
        '      *"SELECT 1"*) echo 1 ;;\n'
        f'      *SchemaMigrations*) cat <<"EOF"\n{migrations}\nEOF\n ;;\n'
        '      *) : ;;\n'
        '    esac\n'
        '    ;;\n'
        '  *) : ;;\n'
        'esac\n'
        'exit 0\n', encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)

    (bin_dir / "pgrep").write_text(
        "#!/usr/bin/env bash\n"
        f'exit {0 if processes_running else 1}\n', encoding="utf-8")
    (bin_dir / "pgrep").chmod(0o755)

    # ClickHouse отвечает пустотой: раздел про данные нас здесь не занимает.
    (bin_dir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n",
                                  encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)

    # psql НА ХОСТЕ намеренно отсутствует — именно так выглядит VPS.
    # Стаб, который «есть», спрятал бы ту самую беду, ради которой спринт.
    env = {**os.environ,
           "PATH": f"{bin_dir}:/usr/bin:/bin",
           "MANTA_TRAIN_ENV": str(tmp_path / "нет.env")}
    proc = subprocess.run(["bash", str(DOCTOR)], capture_output=True,
                          text=True, env=env, cwd=ROOT)
    return proc.stdout + proc.stderr


def section(out: str, title: str) -> str:
    """Кусок вывода от заголовка до следующего заголовка."""
    start = out.index(title)
    rest = out[start + len(title):]
    end = rest.find("\n== ")
    return rest if end < 0 else rest[:end]


def pg_part(out: str) -> str:
    """Только про Postgres: ClickHouse живёт в том же разделе.

    На стабах CH отвечает пустотой и честно даёт свой FAIL. Не отделив
    одно от другого, проверка утверждала бы про PG то, что видит у CH, —
    и была бы красной независимо от чинимого кода.
    """
    return "\n".join(l for l in section(out, "== Миграции").splitlines()
                     if "CH" not in l)


# -- топология сервисов --------------------------------------------------------

def test_containers_count_as_running(tmp_path):
    """ГЛАВНОЕ утверждение спринта: на VPS сервисы живы.

    Без этого doctor на VPS сообщает «11 процессов не запущено» при
    полностью здоровой машине — и делает это ежедневно, в Telegram.
    """
    out = run_doctor(tmp_path, containers_running=True, processes_running=False,
                     migrations="все")
    svc = section(out, "== Сервисы")
    assert "DOWN" not in svc, svc
    for name in SERVICES:
        assert name in svc


def test_host_processes_still_count_as_running(tmp_path):
    """Обратная сторона: дома топология прежняя, и ломать её нельзя.

    Проверка написана так, чтобы годиться обеим машинам. Мутация
    «спрашивать только контейнеры» прошла бы предыдущий тест целиком.
    """
    out = run_doctor(tmp_path, containers_running=False, processes_running=True,
                     migrations="все")
    svc = section(out, "== Сервисы")
    assert "DOWN" not in svc, svc


def test_nothing_running_is_reported(tmp_path):
    """И наоборот: когда сервисов нет нигде, об этом надо сказать.

    Иначе «проверка, которая всегда довольна» — это отсутствие проверки.
    """
    out = run_doctor(tmp_path, containers_running=False, processes_running=False,
                     migrations="все")
    svc = section(out, "== Сервисы")
    assert "DOWN" in svc
    assert "не работают" in svc


def test_down_services_are_named(tmp_path):
    """В предупреждении перечислены ИМЕНА, а не количество.

    «11 процессов не запущено» не говорит, каких именно, и первым делом
    приходится выяснять это руками. Имена ищутся глазами за секунду.
    """
    out = run_doctor(tmp_path, containers_running=False, processes_running=False,
                     migrations="все")
    svc = section(out, "== Сервисы")
    for name in SERVICES:
        assert name in svc.split("не работают")[1]


# -- журнал миграций -----------------------------------------------------------

def test_migrations_are_read_through_the_container(tmp_path):
    """Журнал спрашивается у Postgres в контейнере, а не хостовым psql.

    В PATH этого теста хостового psql нет вовсе — как и на VPS. Верни
    проверка старое поведение, здесь был бы FAIL.
    """
    out = run_doctor(tmp_path, containers_running=True, processes_running=False,
                     migrations="все")
    mig = pg_part(out)
    assert "PG-миграции: журнал полон" in mig, mig
    assert "FAIL" not in mig, mig


def test_unreachable_postgres_is_not_called_an_empty_journal(tmp_path):
    """«Не смог спросить» и «журнал пуст» — разные беды.

    Первая про инструмент, вторая про данные, и лечатся они в разных
    местах. Слитые в одно, они полгода отправляли чинить миграции,
    которые были в полном порядке.
    """
    out = run_doctor(tmp_path, containers_running=True, processes_running=False,
                     pg_in_container=False)
    mig = pg_part(out)
    assert "не смог спросить" in mig, mig
    assert "пуст" not in mig, mig


def test_truly_empty_journal_is_still_a_failure(tmp_path):
    """А настоящий пустой журнал обязан оставаться FAIL.

    Иначе «различили две беды» превратилось бы в «перестали замечать обе»:
    непрогнанные миграции — это база без таблиц, и молчать о них нельзя.
    """
    out = run_doctor(tmp_path, containers_running=True, processes_running=False,
                     pg_in_container=True, migrations=None)
    mig = pg_part(out)
    assert "FAIL" in mig and "пуст" in mig, mig


def test_doctor_does_not_hang_on_an_open_stdin(tmp_path):
    """Проверка здоровья не имеет права зависнуть сама.

    `docker exec -i` отдаёт запущенной команде стандартный ввод. Доктора
    зовут из других скриптов (`daily-report.sh`, `heartbeat.sh`), где на
    входе висит труба, которую никто не закрывает, — и проверка,
    дочитывающая её до конца, встала бы навсегда.

    ЧЕСТНАЯ ОГОВОРКА. В спринте 163 я записал это как случившееся: будто
    прогон набора замер именно из-за `-i`. Проверил потом прямо — с
    возвращённым `-i` набор проходит за те же сорок секунд, а тогдашнее
    «зависание» было двумя моими же параллельными прогонами, деливший
    процессор. Дефект тут не наблюдённый, а возможный; правка всё равно
    верна (данные внутрь не передаются, весь запрос идёт аргументом `-c`),
    но выдавать возможное за случившееся нельзя.

    Труба тут открыта нарочно и не закрывается: ровно так выглядит запуск
    из другого скрипта. Стаб docker дочитывает ввод до конца — то есть
    ждёт вечно, если ему этот ввод отдали.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "exec" ]; then cat >/dev/null; echo 1; exit 0; fi\n'
        'echo true\n', encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    for tool in ("pgrep", "curl"):
        (bin_dir / tool).write_text("#!/usr/bin/env bash\nexit 1\n",
                                    encoding="utf-8")
        (bin_dir / tool).chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(DOCTOR)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin",
             "MANTA_TRAIN_ENV": str(tmp_path / "нет.env")},
        cwd=ROOT)
    try:
        out, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("doctor завис на открытом стандартном вводе")
    assert "== Миграции" in out


def test_no_host_psql_is_used_anywhere(tmp_path):
    """И на уровне текста: хостового psql в скрипте не осталось.

    Проверка выше поймала бы возврат к нему через поведение, но только
    для журнала миграций. Появись хостовый psql в новой проверке — она
    молча не работала бы на VPS, как эта не работала полгода.
    """
    src = DOCTOR.read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "PGPASSWORD" not in body, "дев-пароль в doctor.sh"
    for line in body.splitlines():
        if "psql" in line:
            assert "docker exec" in line, f"psql мимо контейнера: {line.strip()}"
