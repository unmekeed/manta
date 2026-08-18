"""Тесты оффсайт-плеча backup.sh (спринт 76).

Проверяется НЕ весь backup.sh (ему нужен ClickHouse), а поведение
функции upload_cloud в трёх ситуациях. Логика тут ровно та, где легко
ошибиться молча: «оффсайт выключен» и «оффсайт сломан» обязаны
различаться, иначе бэкап выглядит зелёным, а копии в облаке нет.

Функции извлекаются из backup.sh и исполняются в bash со стабом rclone —
так проверяется настоящий код скрипта, а не его пересказ.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKUP_SH = Path(__file__).resolve().parents[1] / "backup.sh"


def _functions() -> str:
    """Вырезать cloud_fail и upload_cloud из backup.sh целиком."""
    src = BACKUP_SH.read_text(encoding="utf-8")
    out = []
    for name in ("cloud_fail", "upload_cloud"):
        m = re.search(rf"^{name}\(\) \{{.*?^\}}", src, re.M | re.S)
        assert m, f"функция {name} не найдена в backup.sh"
        out.append(m.group(0))
    return "\n\n".join(out)


def _run(body: str, env_setup: str = "", stub_rclone: str | None = None):
    """Прогнать тело в bash с извлечёнными функциями. Возвращает
    CompletedProcess; в окружении tg() и состояние замоканы."""
    harness = f"""
set -uo pipefail
BACKUP_DIR=$(mktemp -d); CLOUD_STATE="$BACKUP_DIR/.cloud"; KEEP_DAYS=7
# Метка машины (спринт 142): upload_cloud ограничивает ротацию своими
# слепками, чтобы при обмене между машинами не удалять чужие.
HOST_LABEL=pc1
TG_LOG="$BACKUP_DIR/tg"; : >"$TG_LOG"
tg() {{ echo "$1" >>"$TG_LOG"; }}
{_functions()}
{env_setup}
{body}
"""
    return subprocess.run(["bash", "-c", harness], capture_output=True,
                          text=True)


def test_unset_remote_is_noop():
    """MANTA_CLOUD_REMOTE не задан — оффсайт выключен, rc=0, rclone не зван."""
    r = _run('CLOUD_REMOTE=""; upload_cloud /tmp/x.tar; echo "RC=$?"')
    assert "RC=0" in r.stdout, r.stdout + r.stderr


def test_remote_set_but_rclone_missing_fails_loudly():
    """Задан ремоут, но rclone нет — это «сломано», не «выключено»:
    выход 2 и оранжевый алерт, а не тихий пропуск."""
    r = _run(
        'CLOUD_REMOTE="gdrive:manta"\n'
        '( PATH=/nonexistent upload_cloud /tmp/x.tar ); echo "RC=$?"\n'
        'echo "TG:$(cat "$CLOUD_STATE")"')
    assert "RC=2" in r.stdout, r.stdout + r.stderr
    assert "TG:fail" in r.stdout
    assert "rclone" in r.stderr.lower()


def test_successful_upload_copies_then_prunes():
    """Успех: сначала copy, потом ограниченная нашими файлами ротация,
    состояние ok. Порядок важен — не чистим, если загрузка не прошла."""
    stub = (
        'STUB="$BACKUP_DIR/bin"; mkdir -p "$STUB"\n'
        'printf \'#!/usr/bin/env bash\\necho "rclone $*" >>"$RCLONE_LOG"\\n\' '
        '>"$STUB/rclone"\n'
        'chmod +x "$STUB/rclone"; export RCLONE_LOG="$BACKUP_DIR/rc.log"\n'
        ': >"$RCLONE_LOG"')
    r = _run(
        'CLOUD_REMOTE="gdrive:manta"\n'
        '( PATH="$STUB:$PATH" upload_cloud /tmp/x.tar ); echo "RC=$?"\n'
        'echo "STATE:$(cat "$CLOUD_STATE")"\n'
        'cat "$RCLONE_LOG"',
        env_setup=stub)
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert "STATE:ok" in r.stdout
    lines = [l for l in r.stdout.splitlines() if l.startswith("rclone ")]
    assert lines[0].startswith("rclone copy"), lines
    assert any(l.startswith("rclone delete") for l in lines), lines
    # Ротация ограничена СВОИМИ слепками. Общая маска (спринт 142 её
    # заменил) означала бы, что машина с меньшим KEEP_DAYS удаляет
    # слепки соседа — то есть чинит себе место за счёт чужой истории,
    # причём ровно в той папке, через которую идёт обмен.
    prune = [l for l in lines if l.startswith("rclone delete")][0]
    assert "manta-dataset-pc1-*.tar" in prune, prune


def test_prune_never_runs_before_copy():
    """delete не должен вызываться раньше copy: сломанная загрузка не
    должна тянуть за собой чистку."""
    r = _run(
        'CLOUD_REMOTE="gdrive:manta"\n'
        'STUB="$BACKUP_DIR/bin"; mkdir -p "$STUB"\n'
        'printf \'#!/usr/bin/env bash\\n[ "$1" = copy ] && exit 1\\n'
        'echo "rclone $*" >>"$RCLONE_LOG"\\n\' >"$STUB/rclone"\n'
        'chmod +x "$STUB/rclone"; export RCLONE_LOG="$BACKUP_DIR/rc.log"; '
        ': >"$RCLONE_LOG"\n'
        '( PATH="$STUB:$PATH" upload_cloud /tmp/x.tar ); echo "RC=$?"\n'
        'echo "CALLS:$(cat "$RCLONE_LOG")"')
    assert "RC=2" in r.stdout, r.stdout + r.stderr
    # copy упал → delete не звался вообще.
    assert "CALLS:" in r.stdout
    assert "delete" not in r.stdout.split("CALLS:")[1]
