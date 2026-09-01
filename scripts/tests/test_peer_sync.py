"""Тесты обмена датасетом между машинами (спринт 142).

Скрипт целиком гоняется в bash со стабами rclone и dataset-sync.sh — то
есть проверяется настоящий код, а не его пересказ. Настоящие ClickHouse и
облако для этого не нужны: вся логика, где легко ошибиться, — это разбор
имён и решение «своё/чужое/уже влито».

Дороже всего здесь ошибиться молча. Скрипт, который считает чужой слепок
своим, не падает — он просто ничего не вливает, и обе машины годами
учатся каждая на своей половине датасета.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "peer-sync.sh"
ROOT = Path(__file__).resolve().parents[2]


def run(tmp_path, remote_files, label="pc1", args=(), imported=(),
        import_fails=False, peer_hosts="pc2,pc3,vps-de"):
    """Прогнать peer-sync.sh со стабами. Возвращает (rc, stdout, вызовы)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    calls = tmp_path / "calls.log"

    listing = "\n".join(remote_files)
    (bin_dir / "rclone").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "rclone $*" >> "{calls}"\n'
        'if [ "$1" = "lsf" ]; then\n'
        f'  cat <<"EOF"\n{listing}\nEOF\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "copy" ]; then\n'
        '  # Кладём непустой файл под тем же именем, что и в облаке.\n'
        '  dest="${@: -1}"; src="${@: -2:1}"\n'
        '  echo data > "$dest/$(basename "$src")"\n'
        'fi\n'
        'exit 0\n', encoding="utf-8")
    (bin_dir / "rclone").chmod(0o755)

    # Подставной dataset-sync.sh: настоящий полез бы в докер.
    fake_scripts = tmp_path / "scripts"
    fake_scripts.mkdir()
    (fake_scripts / "dataset-sync.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "import $2" >> "{calls}"\n'
        f'exit {1 if import_fails else 0}\n', encoding="utf-8")
    (fake_scripts / "dataset-sync.sh").chmod(0o755)
    (fake_scripts / "peer-sync.sh").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (fake_scripts / "peer-sync.sh").chmod(0o755)

    if imported:
        peers = backup_dir / "peers"
        peers.mkdir(exist_ok=True)
        (peers / ".imported").write_text(
            "\n".join(imported) + "\n", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MANTA_TRAIN_ENV": str(tmp_path / "нет-такого.env"),
        "MANTA_BACKUP_DIR": str(backup_dir),
        "MANTA_CLOUD_REMOTE": "crypt:manta",
        "MANTA_HOST_LABEL": label,
        "MANTA_PEER_HOSTS": peer_hosts,
        "TELEGRAM_BOT_TOKEN": "",
    }
    proc = subprocess.run(
        ["bash", str(fake_scripts / "peer-sync.sh"), *args],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    log = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return proc.returncode, proc.stdout + proc.stderr, log


TWO = ["manta-dataset-pc1-20260818T0300.tar",
       "manta-dataset-pc2-20260818T0300.tar"]


# -- своё против чужого ----------------------------------------------------------

def test_imports_the_peer_snapshot(tmp_path):
    rc, out, log = run(tmp_path, TWO, label="pc1")
    assert rc == 0
    assert "import" in log
    assert "pc2" in log


def test_never_imports_its_own_snapshot(tmp_path):
    """Свой слепок вливать бессмысленно и вредно.

    Бессмысленно, потому что все эти строки уже в базе. Вредно, потому
    что скачивание гигабайтов каждую ночь выглядит как работающий обмен —
    и настоящая поломка (сосед перестал публиковаться) прячется за этой
    активностью.
    """
    rc, out, log = run(tmp_path, ["manta-dataset-pc1-20260818T0300.tar"],
                       label="pc1")
    assert rc == 0
    assert "import" not in log
    assert "только мои" in out


def test_label_with_dashes_is_parsed_correctly(tmp_path):
    """Метка машины сама может содержать дефисы.

    Разбор «по первому дефису» отрезал бы у метки хвост, машина не узнала
    бы собственный слепок и принялась бы вливать его себе же.
    """
    files = ["manta-dataset-wsl-home-pc-20260818T0300.tar",
             "manta-dataset-vps-de-20260818T0300.tar"]
    rc, out, log = run(tmp_path, files, label="wsl-home-pc")
    assert rc == 0
    assert "vps-de" in log
    assert "wsl-home-pc" not in log.replace("rclone lsf", "")


# -- выбор свежего ---------------------------------------------------------------

def test_takes_the_newest_snapshot_of_each_peer(tmp_path):
    """Из нескольких слепков соседа берётся САМЫЙ СВЕЖИЙ.

    Имя содержит UTC в сортируемом виде, поэтому хронология совпадает с
    лексикографикой. Возьми скрипт первый попавшийся — обмен работал бы,
    но подливал бы недельной давности данные, и отставание заметить было
    бы нечем.
    """
    files = ["manta-dataset-pc2-20260810T0300.tar",
             "manta-dataset-pc2-20260818T0300.tar",
             "manta-dataset-pc2-20260814T0300.tar"]
    rc, out, log = run(tmp_path, files, label="pc1")
    assert "20260818T0300" in log
    assert "20260810" not in log.split("import")[-1]


def test_each_peer_is_imported_when_there_are_several(tmp_path):
    files = ["manta-dataset-pc2-20260818T0300.tar",
             "manta-dataset-pc3-20260818T0300.tar"]
    rc, out, log = run(tmp_path, files, label="pc1")
    imports = [l for l in log.splitlines() if l.startswith("import")]
    assert len(imports) == 2


# -- идемпотентность -------------------------------------------------------------

def test_already_imported_snapshot_is_not_downloaded_again(tmp_path):
    """Повторный прогон не качает то, что уже влито.

    Импорт и сам идемпотентен, но без журнала каждая ночь начиналась бы с
    закачки гигабайтов ради нулевого результата.
    """
    rc, out, log = run(tmp_path, TWO, label="pc1",
                       imported=["manta-dataset-pc2-20260818T0300.tar"])
    assert rc == 0
    assert "import" not in log
    assert "copy" not in log
    assert "уже влит" in out


def test_failed_import_is_not_recorded_as_done(tmp_path):
    """Упавший импорт НЕ попадает в журнал.

    Записать раньше успеха значило бы навсегда пропустить именно тот
    слепок, на котором что-то сломалось, — и обмен молча потерял бы сутки
    чужого сбора.
    """
    rc, out, log = run(tmp_path, TWO, label="pc1", import_fails=True)
    assert rc != 0
    state = tmp_path / "backups" / "peers" / ".imported"
    assert not state.exists() or state.read_text(encoding="utf-8").strip() == ""


# -- чего делать нельзя ----------------------------------------------------------

def test_never_deletes_anything_in_the_cloud(tmp_path):
    """Ни одного rclone delete/purge.

    Ротацию ведёт backup.sh и только по своим файлам. Скрипт, который
    умеет и вливать, и удалять, однажды удалит то, что не успел влить.
    """
    rc, out, log = run(tmp_path, TWO, label="pc1")
    # Смотрим на ПОДКОМАНДУ, а не на подстроку: путь временного каталога
    # содержит имя теста, то есть слово delete, и наивная проверка падала
    # на самой себе.
    subcommands = {l.split()[1] for l in log.splitlines()
                   if l.startswith("rclone ") and len(l.split()) > 1}
    assert subcommands <= {"lsf", "copy"}, f"лишние команды rclone: {subcommands}"


def test_dry_run_downloads_nothing(tmp_path):
    rc, out, log = run(tmp_path, TWO, label="pc1", args=("--dry-run",))
    assert rc == 0
    assert "copy" not in log and "import" not in log


# -- отказы ----------------------------------------------------------------------

def test_missing_remote_is_loud(tmp_path):
    """Ненастроенное облако — ошибка, а не тихий успех.

    «Обмен выключен» и «обмен настроен, но не работает» обязаны
    различаться: во втором случае половина датасета не приезжает, а всё
    выглядит зелёным.
    """
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "MANTA_TRAIN_ENV": str(tmp_path / "нет.env"),
           "MANTA_BACKUP_DIR": str(tmp_path), "MANTA_CLOUD_REMOTE": "",
           "MANTA_PEER_HOSTS": "pc2",
           "TELEGRAM_BOT_TOKEN": ""}
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                          text=True, env=env, cwd=ROOT)
    assert proc.returncode != 0
    assert "MANTA_CLOUD_REMOTE" in proc.stdout + proc.stderr


def test_empty_cloud_is_not_a_failure(tmp_path):
    """Пустое облако — норма первого дня, а не сбой."""
    rc, out, log = run(tmp_path, [], label="pc1")
    assert rc == 0


def test_empty_allowlist_never_means_trust_everyone(tmp_path):
    """Пустой allowlist не означает «доверять всем».

    Само свойство не менялось со спринта 158: без явного списка не
    скачивается и не вливается НИЧЕГО. В спринте 163 изменился только
    вердикт — раньше отказ с красным алертом в Telegram, теперь спокойный
    выход.

    Причина смены: обмен придуман для двух собирающих машин, а с
    2026-09-01 машина одна. Обмен стоял в расписании ежесуточно, то есть
    одиночная машина слала бы «ОБМЕН НЕ УДАЛСЯ» каждую ночь — вечную
    ложную тревогу, которая учит не читать канал.
    """
    rc, out, log = run(tmp_path, TWO, label="pc1", peer_hosts="")
    assert rc == 0
    assert "copy" not in log and "import" not in log
    assert "не настроен" in out, "молчаливый выход неотличим от работы"


def test_disabled_exchange_does_not_alert(tmp_path):
    """И главное: выключенный обмен не трогает Telegram.

    Без этой проверки предыдущая осталась бы зелёной, даже если бы код
    по-прежнему слал алерт: код возврата и наличие сообщения — разные
    вещи, и цена у них разная. Сообщение уходит владельцу в 04:00 каждую
    ночь.
    """
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    calls = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "curl $*" >> "{calls}"\nexit 0\n', encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "MANTA_TRAIN_ENV": str(tmp_path / "нет.env"),
           "MANTA_BACKUP_DIR": str(tmp_path),
           "MANTA_CLOUD_REMOTE": "crypt:manta",
           "MANTA_PEER_HOSTS": "",
           "TELEGRAM_BOT_TOKEN": "секрет", "TELEGRAM_CHAT_ID": "1"}
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                          text=True, env=env, cwd=ROOT)
    assert proc.returncode == 0
    assert not calls.exists(), (
        f"выключенный обмен отправил сообщение: {calls.read_text()}")


def test_unknown_peer_aborts_before_download(tmp_path):
    """Любая новая метка требует явного решения владельца."""
    files = ["manta-dataset-pc2-20260818T0300.tar",
             "manta-dataset-attacker-20260818T0300.tar"]
    rc, out, log = run(tmp_path, files, label="pc1", peer_hosts="pc2")
    assert rc != 0
    assert "неизвестная метка отправителя: attacker" in out
    assert "copy" not in log and "import" not in log


def test_allowlist_matches_whole_labels(tmp_path):
    """pc2 не должен разрешать pc20 по совпавшему префиксу."""
    files = ["manta-dataset-pc20-20260818T0300.tar"]
    rc, out, log = run(tmp_path, files, label="pc1", peer_hosts="pc2")
    assert rc != 0
    assert "pc20" in out
    assert "copy" not in log


def test_peer_snapshots_never_land_next_to_our_own(tmp_path):
    """Чужие слепки не попадают в каталог своих бэкапов.

    Здесь ломается не обмен, а СТОРОЖ. heartbeat.sh считает свежесть
    бэкапа по самому новому файлу каталога, backup-drill.sh
    восстанавливает самый новый, backup.sh по нему ротирует. Слепок
    соседа, положенный рядом, был бы самым новым каждую ночь — и сторож
    бодро докладывал бы о свежем бэкапе, когда свой не снимается уже
    сутки. Такая тишина в этом проекте однажды длилась тринадцать дней.
    """
    rc, out, log = run(tmp_path, TWO, label="pc1")
    assert rc == 0
    backups = tmp_path / "backups"
    assert list(backups.glob("manta-dataset-*.tar")) == []
    assert (backups / "peers").is_dir()


def test_imported_archive_is_removed_but_remembered(tmp_path):
    """После импорта архив удаляется, а запись о нём остаётся.

    Слепки идут гигабайтами, и копить чужие на диске, где место уже
    кончалось, незачем: всё их содержимое уже в базе. А вот забыть, что
    файл влит, нельзя — иначе следующая ночь начнётся с закачки того же
    самого.
    """
    rc, out, log = run(tmp_path, TWO, label="pc1")
    peers = tmp_path / "backups" / "peers"
    assert list(peers.glob("*.tar")) == []
    assert "manta-dataset-pc2-20260818T0300.tar" in \
        (peers / ".imported").read_text(encoding="utf-8")
