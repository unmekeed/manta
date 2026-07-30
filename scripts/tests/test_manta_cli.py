"""Тесты диспетчера scripts/manta (спринт 77).

Проверяется решающая логика `manta up`, где легко навредить: когда
восстанавливать бэкап (только на пустом датасете, иначе затрём свежее
старым) и откуда его брать (локальный в приоритете над облачным).

Функции извлекаются из scripts/manta и исполняются в bash со стабами
ch_matches / dataset-sync / rclone — проверяется настоящий код скрипта.
"""
import re
import subprocess
from pathlib import Path

MANTA = Path(__file__).resolve().parents[1] / "manta"


def _functions(*names: str) -> str:
    src = MANTA.read_text(encoding="utf-8")
    out = []
    for name in names:
        m = re.search(rf"^{name}\(\) \{{.*?^\}}", src, re.M | re.S)
        assert m, f"функция {name} не найдена в scripts/manta"
        out.append(m.group(0))
    return "\n\n".join(out)


def _run(body: str, setup: str = ""):
    harness = f"""
set -uo pipefail
{_functions("find_backup", "restore_if_empty")}
{setup}
{body}
"""
    return subprocess.run(["bash", "-c", harness], capture_output=True,
                          text=True)


def test_populated_dataset_skips_restore():
    """Датасет не пуст → восстановление НЕ запускается: иначе на рабочей
    машине бэкап регулярно подмешивал бы устаревшие строки."""
    r = _run(
        'ch_matches() { echo 4156; }\n'
        'RESTORE_THRESHOLD=100\n'
        'restore_if_empty')
    assert "не пуст (4156" in r.stdout, r.stdout + r.stderr
    assert "восстанавливаю" not in r.stdout


def test_empty_dataset_triggers_restore():
    """Пустой датасет + доступный бэкап → импорт запускается."""
    setup = (
        'ch_matches() { echo 0; }\n'
        'RESTORE_THRESHOLD=100\n'
        'BK=$(mktemp); echo data >"$BK"\n'
        'find_backup() { echo "$BK"; }\n'
        # стаб dataset-sync: фиксируем факт вызова import
        'mkdir -p ./scripts\n'
        'cat >./scripts/dataset-sync.sh <<EOF\n'
        '#!/usr/bin/env bash\necho "IMPORT $2" >>"$BK.log"\nEOF\n'
        'chmod +x ./scripts/dataset-sync.sh')
    r = _run('restore_if_empty', setup)
    assert "почти пуст (0" in r.stdout, r.stdout + r.stderr
    assert "восстанавливаю" in r.stdout


def test_empty_dataset_no_backup_starts_clean():
    """Пусто и бэкапа нет → не падаем, стартуем с чистого датасета."""
    r = _run(
        'ch_matches() { echo 0; }\n'
        'RESTORE_THRESHOLD=100\n'
        'find_backup() { echo ""; }\n'
        'restore_if_empty; echo "RC=$?"')
    assert "бэкапа не найдено" in r.stdout, r.stdout + r.stderr
    assert "RC=0" in r.stdout


def test_local_backup_preferred_over_cloud(tmp_path):
    """Локальный слепок берётся раньше облачного: он всегда новее (в облако
    уезжает уже ПОСЛЕ локального). rclone не должен вызываться вовсе."""
    d = tmp_path / "backups"
    d.mkdir()
    local = d / "manta-dataset-20260727T1200.tar"
    local.write_text("x")
    r = _run(
        f'MANTA_BACKUP_DIR="{d}"\n'
        'MANTA_CLOUD_REMOTE="gdrive:manta"\n'
        # rclone-стаб, который «падает», если его позовут: локальный путь
        # не должен до него дойти.
        'rclone() { echo "RCLONE ВЫЗВАН" >&2; exit 1; }\n'
        'export -f rclone\n'
        'find_backup')
    assert str(local) in r.stdout, r.stdout + r.stderr
    assert "RCLONE ВЫЗВАН" not in r.stderr


def test_cloud_used_when_no_local(tmp_path):
    """Локального нет → качаем из облака через rclone (lsf → copy), берём
    самый свежий по имени. Стаб rclone пишем файлом, чтобы не воевать с
    экранированием heredoc."""
    d = tmp_path / "empty"
    d.mkdir()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "rclone").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "  lsf) echo manta-dataset-20260726T0000.tar;"
        " echo manta-dataset-20260727T0000.tar ;;\n"
        "  copy) mkdir -p \"$3\"; echo tar > \"$3/$(basename \"$2\")\" ;;\n"
        "esac\n")
    (stub_dir / "rclone").chmod(0o755)
    r = _run(
        'find_backup',
        f'MANTA_BACKUP_DIR="{d}"\n'
        'MANTA_CLOUD_REMOTE="gdrive-crypt:"\n'
        f'export PATH="{stub_dir}:$PATH"')
    assert "manta-dataset-20260727T0000.tar" in r.stdout, r.stdout + r.stderr
