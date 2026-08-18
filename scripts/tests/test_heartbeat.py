"""Тесты сторожа (scripts/heartbeat.sh, спринт 138).

Задача поставлена живым случаем: бэкапы на машине встали 5 августа, и
никто не узнал об этом тринадцать дней. Алерты были — они просто не
могли сработать:

  * backup.sh шлёт сообщение только на ПЕРВОМ сбое, дальше состояние уже
    «fail» и повторов нет;
  * если скрипт не запустился вовсе, сбоя нет — значит нет и алерта;
  * tg() молча возвращает успех при незаданном токене.

Отсюда главное свойство сторожа, которое здесь и проверяется: он шлёт
сообщение КАЖДЫЙ прогон, а не только при поломке. Тогда его отсутствие
само становится сигналом — а это единственный способ заметить машину,
которая вообще перестала просыпаться.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
HEARTBEAT = SCRIPTS / "heartbeat.sh"
SRC = HEARTBEAT.read_text(encoding="utf-8")


def _run(tmp_path: Path, *, doctor_code: int = 0, doctor_out: str = "",
         backups: list[str] | None = None, token: bool = True,
         backup_age_days: int = 0):
    """Прогон с подменёнными doctor.sh и curl.

    Настоящий doctor требует контейнеров, а настоящий curl — сети и
    боевого токена. Подменяем оба: проверяется наше поведение, а не
    Telegram и не Docker.
    """
    import shutil
    import time

    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(HEARTBEAT, work / "scripts" / "heartbeat.sh")

    doctor = work / "scripts" / "doctor.sh"
    doctor.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{doctor_out}\nEOF\n"
                      f"exit {doctor_code}\n", encoding="utf-8")
    doctor.chmod(0o755)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for name in (backups or []):
        f = backup_dir / name
        f.write_bytes(b"x" * 10)
        old = time.time() - backup_age_days * 86400
        os.utime(f, (old, old))

    # curl-заглушка: пишет, что бы ушло в Telegram, и не ходит в сеть.
    binx = tmp_path / "bin"
    binx.mkdir()
    sent = tmp_path / "sent.txt"
    (binx / "curl").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{sent}"\nexit 0\n',
        encoding="utf-8")
    (binx / "curl").chmod(0o755)

    env = {**os.environ,
           "PATH": f"{binx}:{os.environ['PATH']}",
           "MANTA_TRAIN_ENV": "/nonexistent",
           "MANTA_BACKUP_DIR": str(backup_dir),
           "MANTA_HOST_LABEL": "тестовая-машина"}
    if token:
        env["TELEGRAM_BOT_TOKEN"] = "тестовый-токен"
        env["TELEGRAM_CHAT_ID"] = "123"
    else:
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("TELEGRAM_CHAT_ID", None)

    proc = subprocess.run(["bash", str(work / "scripts" / "heartbeat.sh")],
                          cwd=work, env=env, capture_output=True, text=True,
                          timeout=60)
    delivered = sent.read_text(encoding="utf-8") if sent.exists() else ""
    return proc, delivered


HEALTHY = "== всё\n    OK  всё хорошо\n>> ЗДОРОВ (warn: 0)"
BROKEN = ("== данные\n   FAIL ReplayEvents: последняя вставка 124ч назад\n"
          "   FAIL витрина не растёт 124ч (9882 матчей)\n"
          ">> ПРОБЛЕМ: 2 (warn: 0)")


# -- главное свойство: сообщение приходит ВСЕГДА -----------------------------------

def test_message_is_sent_even_when_everything_is_fine(tmp_path):
    """Молчание должно означать «сторож умер», а не «всё хорошо».

    Это и есть защита от случая, который случился: машина перестала
    просыпаться, и отсутствие алертов выглядело нормой. Работает только
    если сообщение ждут КАЖДЫЙ день.
    """
    proc, delivered = _run(tmp_path, doctor_code=0, doctor_out=HEALTHY,
                           backups=["manta-dataset-20260818T0000.tar"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert delivered, "при здоровом состоянии сообщение не отправлено"
    assert "всё в норме" in delivered


def test_message_is_sent_when_broken(tmp_path):
    proc, delivered = _run(tmp_path, doctor_code=1, doctor_out=BROKEN,
                           backups=["manta-dataset-20260818T0000.tar"])
    assert proc.returncode == 1
    assert "есть проблемы" in delivered


def test_problems_from_doctor_reach_the_message(tmp_path):
    """В сообщение попадают строки FAIL, а не весь вывод.

    Полный doctor — это экран текста, в мессенджере он нечитаем. Нужен
    список проблем, по которому сразу понятно, ехать чинить или нет.
    """
    _, delivered = _run(tmp_path, doctor_code=1, doctor_out=BROKEN,
                        backups=["manta-dataset-20260818T0000.tar"])
    assert "ReplayEvents" in delivered
    assert "витрина не растёт" in delivered
    assert "OK" not in delivered.replace("ok", ""), "в сообщение попал шум"


# -- проверка отсутствия, которой нет в doctor -------------------------------------

def test_stale_backup_is_a_problem_even_if_doctor_is_happy(tmp_path):
    """Протухший бэкап — беда, о которой doctor не знает.

    Ровно этот случай и произошёл: конвейер работал, данные шли, а
    бэкапов не было две недели. doctor смотрит на данные и не смотрит на
    копии.
    """
    proc, delivered = _run(tmp_path, doctor_code=0, doctor_out=HEALTHY,
                           backups=["manta-dataset-20260801T0000.tar"],
                           backup_age_days=13)
    assert proc.returncode == 1, "старый бэкап не сделал прогон проблемным"
    assert "последний бэкап 13 суток назад" in delivered


def test_fresh_backup_is_not_a_problem(tmp_path):
    proc, delivered = _run(tmp_path, doctor_code=0, doctor_out=HEALTHY,
                           backups=["manta-dataset-20260818T0000.tar"],
                           backup_age_days=0)
    assert proc.returncode == 0
    assert "всё в норме" in delivered


def test_no_backups_at_all_is_reported(tmp_path):
    proc, delivered = _run(tmp_path, doctor_code=0, doctor_out=HEALTHY,
                           backups=[])
    assert proc.returncode == 1
    assert "бэкапов нет" in delivered


# -- ненастроенный канал --------------------------------------------------------

def test_missing_token_fails_loudly(tmp_path):
    """Нет токена — это «сторож не сторожит», а не «уведомления выключены».

    tg() в backup.sh в этом случае молча возвращает успех, и
    ненастроенные уведомления выглядят как настроенные. Здесь наоборот:
    громкий отказ с ненулевым кодом, чтобы cron это заметил.
    """
    proc, delivered = _run(tmp_path, doctor_code=0, doctor_out=HEALTHY,
                           backups=["manta-dataset-20260818T0000.tar"],
                           token=False)
    assert proc.returncode == 2
    assert "не сторожит" in proc.stderr or "бесполезен" in proc.stderr
    assert not delivered


def test_host_label_is_in_the_message(tmp_path):
    """Машин несколько — сообщение обязано говорить, чья это беда."""
    _, delivered = _run(tmp_path, doctor_code=1, doctor_out=BROKEN,
                        backups=["manta-dataset-20260818T0000.tar"])
    assert "тестовая-машина" in delivered


# -- устройство -------------------------------------------------------------------

def test_built_on_top_of_doctor_not_beside_it():
    """Сторож не заводит второй источник истины о здоровье.

    Два независимых мнения о том, здоров ли конвейер, разойдутся — и
    непонятно будет, какому верить.
    """
    assert "./scripts/doctor.sh" in SRC
    for own in ("clickhouse-client", "psql", "SELECT"):
        assert own not in SRC, f"сторож сам лезет в базы: {own}"
