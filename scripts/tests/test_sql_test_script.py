"""scripts/sql-test.sh — прогон SQL-тестов в образе (спринт 152).

ЗАЧЕМ СКРИПТ. Часть проверок бессмысленна без настоящей базы: смысл живёт
в предикате запроса, а фейковый курсор возвращает то, что в него
положили. Такие тесты в проекте есть, но команда для их запуска
собиралась по памяти — и трижды подряд собиралась неверно:

    make                на VPS не установлен вовсе
    pytest              на хосте нет и быть не должно
    python:3.11-slim    голый образ падал на импорте confluent_kafka

Тот же урок, что у миграций в спринте 143: инструмент берут оттуда, где
он уже есть.

ЗАЧЕМ ЭТИ ТЕСТЫ. Скрипт собирает команду из четырёх добытых на месте
значений — образ, сеть, пароль, список тестов, — и ошибка в любом из них
проявится только на машине с docker. Здесь docker подставной: он
записывает, ЧТО у него спросили и с чем запустили, и проверяется именно
это. Своего docker в песочнице нет, и без подстановки файл проверял бы
лишь то, что скрипт не падает на разборе.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sql-test.sh"

# Что подставной docker отвечает на inspect -f ФОРМАТ ИМЯ. Формат лежит в
# $3, а имя в $4 — в спринте 143 стаб путал их местами и молча отвечал
# пустотой, из-за чего все контейнеры выглядели живыми.
STUB = r"""#!/usr/bin/env bash
echo "docker $*" >>"$DOCKER_LOG"
case "$1" in
  inspect)
    if [ "$2" = "-f" ]; then
      case "$3" in
        *NetworkSettings*) echo "manta_default" ;;
        *Config.Image*)    echo "manta-data-collector:latest" ;;
      esac
    fi
    # Без -f это проверка существования: код 0, если имя в списке.
    for name in $KNOWN_CONTAINERS; do [ "$2" = "$name" ] && exit 0; done
    [ "$2" = "-f" ] && exit 0
    exit 1
    ;;
  exec)
    # docker exec ИМЯ printenv ПЕРЕМЕННАЯ: имя в $2, printenv в $3,
    # переменная в $4. Первая версия стаба читала $3 и отвечала пустотой
    # на всё — ровно та ошибка, что была в стабе спринта 143, где
    # перепутаны местами формат и имя контейнера.
    case "$4" in
      POSTGRES_USER)     echo "dota" ;;
      POSTGRES_PASSWORD) echo "$FAKE_PASSWORD" ;;
      POSTGRES_DB)       echo "manta" ;;
    esac
    exit 0
    ;;
  run) exit 0 ;;
esac
exit 0
"""


def run(tmp_path, args=(), *, known="manta-postgres-1 manta-data-collector-1",
        password="s3cret", env_extra=None):
    """Запустить скрипт с подставным docker; вернуть (код, вывод, журнал)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("bash", "head", "ls", "printf", "cd", "dirname", "pwd"):
        real = shutil.which(tool)
        if real and not (bin_dir / tool).exists():
            (bin_dir / tool).symlink_to(real)
    stub = bin_dir / "docker"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)

    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "DOCKER_LOG": str(log),
        "KNOWN_CONTAINERS": known,
        "FAKE_PASSWORD": password,
    }
    env.update(env_extra or {})
    proc = subprocess.run([shutil.which("bash"), str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr, log.read_text("utf-8")


def test_script_is_executable_and_parses():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "скрипт без бита исполнения"
    subprocess.run([shutil.which("bash"), "-n", str(SCRIPT)], check=True)


def test_runs_the_tests_in_the_app_image(tmp_path):
    """Образ берётся у ЖИВОГО контейнера, а не пишется в скрипте.

    Записанное имя разошлось бы с реальностью при первом же переименовании
    проекта compose, и разошлось бы молча: docker сказал бы «нет такого
    образа» после того, как всё уже настроено.
    """
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"])
    assert code == 0, out
    run_line = [l for l in log.splitlines() if l.startswith("docker run")]
    assert run_line, f"docker run не вызывался:\n{log}"
    assert "manta-data-collector:latest" in run_line[0]


def test_password_comes_from_the_container_not_a_file(tmp_path):
    """Пароль спрашивают у Postgres, а не разбирают env-файл.

    Файл на машине может содержать несколько строк с одним именем, разные
    имена для одного значения или не содержать ничего — всё три случая уже
    случались. У запущенного контейнера пароль ровно один, и это тот,
    который он принимает.
    """
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"],
                         password="пароль-из-контейнера")
    assert code == 0, out
    assert "exec manta-postgres-1 printenv POSTGRES_PASSWORD" in log
    run_line = next(l for l in log.splitlines() if l.startswith("docker run"))
    assert "пароль-из-контейнера" in run_line


def test_connects_over_the_compose_network_by_container_name(tmp_path):
    """Ходим по имени контейнера в его же сети.

    Через опубликованный порт было бы короче, но на VPS он привязан к
    127.0.0.1, а --network host ведёт себя по-разному на разных установках
    docker. Сеть спрашивается у самого Postgres.
    """
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"])
    assert code == 0, out
    run_line = next(l for l in log.splitlines() if l.startswith("docker run"))
    assert "--network manta_default" in run_line
    assert "@manta-postgres-1:5432" in run_line
    assert "--network host" not in run_line


def test_runs_as_root_because_the_image_is_nobody(tmp_path):
    """Без --user root pip не запишет: образ работает под nobody."""
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"])
    assert code == 0, out
    run_line = next(l for l in log.splitlines() if l.startswith("docker run"))
    assert "--user root" in run_line


def test_repo_is_mounted_whole(tmp_path):
    """Монтируется весь репозиторий, а не каталог приложения.

    Тестам нужны и libs (импорты по голому имени), и infra/migrations —
    оттуда берётся НАСТОЯЩАЯ схема, а не переписанная в тест копия.
    """
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"])
    assert code == 0, out
    run_line = next(l for l in log.splitlines() if l.startswith("docker run"))
    assert f"-v {ROOT}:/repo" in run_line
    assert "/repo/libs" in run_line


def test_without_arguments_it_finds_every_sql_test(tmp_path):
    """Список тестов не записан в скрипте.

    Записанный, он отстал бы ровно тогда, когда добавляют новый SQL-тест,
    — и новый оказался бы единственным, который никогда не запускается.
    """
    code, out, log = run(tmp_path)
    assert code == 0, out
    run_line = next(l for l in log.splitlines() if l.startswith("docker run"))
    assert "tests/test_dedup_sql.py" in run_line
    assert "tests/test_candidates_sql.py" in run_line


@pytest.mark.parametrize("missing", ["manta-postgres-1",
                                     "manta-data-collector-1"])
def test_missing_container_stops_with_a_named_reason(tmp_path, missing):
    """Отсутствие контейнера названо поимённо, а не выражено кодом выхода.

    Молчаливый отказ здесь особенно дорог: SQL-тест и так пропускается
    без базы, и «ничего не произошло» слишком легко принять за «прошло».
    """
    known = " ".join(n for n in ("manta-postgres-1", "manta-data-collector-1")
                     if n != missing)
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"], known=known)
    assert code != 0, "отсутствие контейнера прошло незамеченным"
    assert "ОСТАНОВ" in out and missing in out
    assert not any(l.startswith("docker run") for l in log.splitlines()), \
        "тесты запущены при недоступной базе"


def test_empty_password_stops_instead_of_connecting_anonymously(tmp_path):
    """Пустой пароль — не «пароля нет», а «спросить не удалось».

    Уйди он в DSN как есть, psycopg попробовал бы соединиться без пароля,
    и мы получили бы «Authentication failed» вместо внятной причины.
    """
    code, out, log = run(tmp_path, ["tests/test_dedup_sql.py"], password="")
    assert code != 0
    assert "POSTGRES_PASSWORD" in out
    assert not any(l.startswith("docker run") for l in log.splitlines())
