"""Запуск инструмента внутри образа (спринт 208).

Проверяется ПОВЕДЕНИЕ, а не текст скрипта: подставной `docker`
записывает, чем его звали, и отвечает на `inspect` тем, что ему велено.
Статическая проверка здесь была бы слепа ровно к тому, что сломалось, —
`make ml-status` содержал и правильный модуль, и правильный PYTHONPATH, и
всё равно падал, потому что бежал не там.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "in-image.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
STATE="$MANTA_FAKE_STATE"
case "${1:-}" in
inspect)
    # docker inspect -f '{{.State.Running}}' <имя>
    name="${!#}"
    for r in ${MANTA_FAKE_RUNNING:-}; do
        [ "$r" = "$name" ] && { echo true; exit 0; }
    done
    echo "Error: No such object: $name" >&2
    exit 1;;
exec)
    shift
    printf '%s\n' "$*" >> "$STATE/execs"
    exit "${MANTA_FAKE_EXIT:-0}";;
esac
exit 99
"""

# Подставной хостовый интерпретатор. Существует ровно затем, чтобы
# доказать, что его НЕ ЗВАЛИ: мутация «при отсутствующем контейнере
# откатиться на python3» пережила первую редакцию этих тестов — отказ
# оставался ненулевым и сообщения на месте, потому что хостовый python
# падал сам, только уже на ModuleNotFoundError. То есть тест проверял
# форму отказа, а не то, что подмены нет.
FAKE_PYTHON = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$MANTA_FAKE_STATE/host-python"
exit 1
"""


def run(tmp_path, args, running="manta-ml-service-1 manta-ml-autotrain-1",
        exit_code="0"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("docker", FAKE_DOCKER), ("python3", FAKE_PYTHON),
                       ("python", FAKE_PYTHON)):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["MANTA_FAKE_STATE"] = str(state)
    env["MANTA_FAKE_RUNNING"] = running
    env["MANTA_FAKE_EXIT"] = exit_code

    r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                       text=True, env=env, timeout=60)
    execs = (state / "execs").read_text(encoding="utf-8").splitlines() \
        if (state / "execs").exists() else []
    run.host_python = (state / "host-python").exists()
    return r, execs


def test_the_module_and_its_arguments_reach_the_container(tmp_path):
    """Аргументы доезжают до образа без потерь.

    Проглоченный `--trials 60` не падает: обучение просто пройдёт с
    другими настройками, и разбираться в этом будут по результату.
    """
    r, execs = run(tmp_path, ["ml-write", "-m", "training.tune",
                              "--trials", "60", "--apply"])
    assert r.returncode == 0, r.stderr
    assert len(execs) == 1, execs
    assert execs[0] == ("manta-ml-autotrain-1 python -m training.tune "
                        "--trials 60 --apply"), execs[0]


def test_reading_and_writing_go_to_different_containers(tmp_path):
    """ГЛАВНОЕ ПРО РАЗДЕЛЕНИЕ: обучение не идёт в контейнер инференса.

    У ml-service ключ реестра только на чтение и лимит памяти 768 МиБ.
    Запущенное через exec обучение считалось бы в ЕГО cgroup: OOM убил бы
    не обучение, а сам сервис инференса, и вместе с ним генерацию
    отчётов.
    """
    _, read_ = run(tmp_path / "r", ["ml-read", "-m", "training.status"])
    _, write = run(tmp_path / "w", ["ml-write", "-m", "training.auto"])
    assert read_[0].split()[0] != write[0].split()[0], (
        f"чтение и обучение идут в один контейнер: {read_[0]}")
    assert read_[0].startswith("manta-ml-service-1")
    assert write[0].startswith("manta-ml-autotrain-1")


def test_a_stopped_container_is_named_and_nothing_is_run(tmp_path):
    """ГЛАВНОЕ: подмены хостом нет.

    Откат на `python3` при отсутствующем контейнере вернул бы ровно тот
    отказ, ради которого скрипт написан, только уже молча и в
    неожиданном месте. Отказ обязан быть отказом и назвать лечение.
    """
    r, execs = run(tmp_path, ["ml-read", "-m", "training.status"], running="")
    assert r.returncode != 0
    assert execs == [], "команда всё-таки выполнена"
    # САМОЕ ГЛАВНОЕ УТВЕРЖДЕНИЕ ЭТОГО ФАЙЛА, и оно не про код возврата.
    # Мутация «откатиться на python3» пережила первую редакцию теста:
    # хостовый интерпретатор падал сам, отказ оставался ненулевым, обе
    # строки лечения печатались — и подмена была невидима.
    assert not run.host_python, (
        "при отсутствующем контейнере запущен хостовый python — это и "
        "есть тот отказ, ради которого скрипт написан, только молча")
    assert "manta-ml-service-1" in r.stderr, r.stderr
    assert "make vps-up" in r.stderr, "не сказано, чем лечить"


def test_the_exit_code_of_the_tool_survives(tmp_path):
    """Код возврата инструмента доходит до вызывающего.

    Проглоти скрипт ненулевой код — `make ml-train` рапортовал бы об
    успехе при провалившемся обучении. Ровно тот род молчаливого отказа,
    которым этот проект занят весь сентябрь.
    """
    r, _ = run(tmp_path, ["ml-write", "-m", "training.auto"], exit_code="7")
    assert r.returncode == 7, r.returncode


def test_an_unknown_role_is_refused(tmp_path):
    """Опечатка в роли — отказ, а не «как-нибудь запустим».

    Молчаливый выбор контейнера по умолчанию отправил бы обучение в
    сервис инференса — то самое, что тест выше и запрещает.
    """
    r, execs = run(tmp_path, ["ml-wrote", "-m", "training.auto"])
    assert r.returncode != 0 and execs == []


def test_the_container_can_be_overridden(tmp_path):
    """Имя контейнера переопределяется.

    Машина с другим префиксом compose — не повод править Makefile:
    именование проекта не свойство инструмента.
    """
    env_name = "manta2-ml-service-1"
    os.environ["ML_READ_CONTAINER"] = env_name
    try:
        _, execs = run(tmp_path, ["ml-read", "-m", "training.status"],
                       running=env_name)
    finally:
        del os.environ["ML_READ_CONTAINER"]
    assert execs[0].startswith(env_name), execs
