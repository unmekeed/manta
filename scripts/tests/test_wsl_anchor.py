"""Тесты якоря WSL (спринт 137).

Якорь нужен для одного: чтобы окно терминала можно было закрыть на
крестик, не погасив стек. Работает он в единственном экземпляре и переживает
холостые срабатывания задачи Планировщика, поэтому проверяем не «скрипт
запускается», а именно жизненный цикл: поднялся → виден → второй раз не
поднялся → снялся.

Тесты запускают настоящий процесс: подменять его моком нечего, вся суть
якоря в том, что процесс РЕАЛЬНО живёт.
"""
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ANCHOR = Path(__file__).resolve().parents[1] / "wsl-anchor.sh"


@pytest.fixture
def logs():
    d = tempfile.mkdtemp(prefix="manta-anchor-test-")
    yield Path(d)
    # Якорь — живой процесс: если тест упал, не оставляем его в системе.
    subprocess.run([str(ANCHOR), "stop"],
                   env={**os.environ, "MANTA_LOG_DIR": d},
                   capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def _run(logs: Path, *args, timeout: int = 10):
    return subprocess.run(
        [str(ANCHOR), *args],
        env={**os.environ, "MANTA_LOG_DIR": str(logs)},
        capture_output=True, text=True, timeout=timeout)


def _start(logs: Path) -> None:
    """Поднять якорь в фоне, как это делает задача Планировщика.

    Ждём именно НУЛЕВОГО кода `status`, а не появления pidfile: в тесте на
    мусорный pidfile файл уже существует к моменту запуска, и ожидание по
    существованию возвращалось бы мгновенно — до того, как настоящий якорь
    успел перезаписать номер.
    """
    subprocess.Popen(
        [str(ANCHOR), "run"],
        env={**os.environ, "MANTA_LOG_DIR": str(logs)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    for _ in range(50):
        if _run(logs, "status").returncode == 0:
            return
        time.sleep(0.1)
    raise AssertionError("якорь не поднялся за 5 секунд")


def _anchor_pids() -> list[int]:
    """Живые якоря в системе — по argv[0], как их видит `ps -o args`."""
    out = subprocess.run(["ps", "-eo", "pid,args"],
                         capture_output=True, text=True).stdout
    return [int(line.split()[0]) for line in out.splitlines()
            if line.split()[1:2] == ["manta-anchor"]]


def test_status_reports_absence_with_nonzero_code(logs):
    """Нет якоря — ненулевой код: цель make обязана падать, а не молчать."""
    r = _run(logs, "status")
    assert r.returncode == 1
    assert "НЕ работает" in r.stdout


def test_anchor_starts_and_is_reported_running(logs):
    before = set(_anchor_pids())
    _start(logs)
    r = _run(logs, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "работает" in r.stdout
    assert "крестик" in r.stdout
    new = set(_anchor_pids()) - before
    assert len(new) == 1, f"ожидался ровно один новый якорь, стало {new}"


def test_second_start_does_not_spawn_a_second_anchor(logs):
    """Холостое срабатывание задачи Планировщика — норма, а не ошибка.

    Задача повторяется по расписанию (самолечение после `wsl --shutdown`),
    поэтому повторный запуск обязан выйти с нулевым кодом и НЕ поднять
    второй якорь: иначе за сутки их накопились бы десятки.
    """
    _start(logs)
    first = set(_anchor_pids())
    r = _run(logs, "run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_anchor_pids()) == first


def test_stop_removes_the_anchor(logs):
    _start(logs)
    pid = int((logs / "wsl-anchor.pid").read_text().strip())
    assert pid in _anchor_pids()
    r = _run(logs, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    for _ in range(50):
        if pid not in _anchor_pids():
            break
        time.sleep(0.1)
    assert pid not in _anchor_pids()
    assert _run(logs, "status").returncode == 1


def test_second_start_is_blocked_even_without_the_lockfile(logs):
    """Защит от второго якоря две, и проверяются они по отдельности.

    Обычно повтор ловит flock: дескриптор блокировки переживает exec, и
    файл остаётся заперт всё время жизни якоря. Поэтому проверка pidfile
    рядом выглядит лишней — ровно до того момента, когда файл блокировки
    удалят. Достаточно почистить каталог логов, и flock на новом файле
    возьмётся сразу: замок остался на старом inode, а нового сторожа у
    него нет.

    Тогда единственным препятствием остаётся pidfile. Без него в системе
    появился бы второй якорь при каждом срабатывании задачи Планировщика.
    """
    _start(logs)
    first = set(_anchor_pids())
    (logs / "wsl-anchor.lock").unlink()
    r = _run(logs, "run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_anchor_pids()) == first, "поднялся второй якорь"


def test_stop_kills_promptly_not_eventually(logs):
    """`stop` обязан убить якорь СРАЗУ, а не «когда-нибудь».

    Ровно здесь пряталась настоящая ошибка. Тело якоря было
    `trap "exit 0" TERM; while true; do sleep 3600; done`, а bash не
    выполняет обработчик сигнала, пока не завершится текущая команда
    переднего плана — то есть SIGTERM ждал конца часового sleep. `stop`
    печатал «якорь снят», стирал pidfile и возвращал ноль, а процесс
    оставался жить: status его уже не видел, и каждый цикл «снял —
    поднял» добавлял в систему по лишнему якорю.

    Первая версия этого файла дефект НЕ ловила: тесты подставляли
    короткий такт sleep через переменную окружения, и на нём обработчик
    успевал сработать в отведённое окно. Проверялась конфигурация, в
    которой система не работает. Поэтому переменной больше нет, а
    ожидание здесь заведомо короче любого мыслимого такта.
    """
    _start(logs)
    pid = int((logs / "wsl-anchor.pid").read_text().strip())
    _run(logs, "stop")
    time.sleep(1.0)
    assert pid not in _anchor_pids(), (
        "якорь пережил stop — обработчик сигнала ждёт конца sleep")


def test_stop_leaves_no_anchor_behind_across_cycles(logs):
    """Десять циклов «поднял — снял» не оставляют ни одного якоря.

    Накопление проявляется именно так: по одному процессу за цикл, каждый
    невидимый для status, потому что pidfile уже стёрт.
    """
    before = set(_anchor_pids())
    for _ in range(10):
        _start(logs)
        _run(logs, "stop")
    time.sleep(1.0)
    assert set(_anchor_pids()) - before == set()


def test_stale_pidfile_does_not_block_a_restart(logs):
    """Мусорный pidfile после `wsl --shutdown` не должен запирать якорь.

    Дистрибутив гаснет, не разбирая файлы; pidfile остаётся, а номер после
    перезагрузки достаётся чужому процессу. Если бы проверка живости
    смотрела только на существование /proc/<pid>, якорь считался бы
    поднятым навсегда и стек больше не переживал бы закрытие окна — ровно
    та поломка, от которой этот скрипт и защищает.
    """
    # PID 1 существует всегда и якорем не является.
    (logs / "wsl-anchor.pid").write_text("1\n")
    assert _run(logs, "status").returncode == 1
    _start(logs)
    assert _run(logs, "status").returncode == 0


def test_garbage_pidfile_is_survived(logs):
    """Битый pidfile не должен ронять скрипт с трассировкой bash."""
    for junk in ("", "\n", "не число", "-5", "99999999999999999999"):
        (logs / "wsl-anchor.pid").write_text(junk)
        r = _run(logs, "status")
        assert r.returncode == 1, f"на {junk!r}: код {r.returncode}"
        assert "НЕ работает" in r.stdout, f"на {junk!r}: {r.stdout!r}"


def test_unknown_command_is_rejected(logs):
    r = _run(logs, "поехали")
    assert r.returncode == 2
    assert "использование" in r.stderr
