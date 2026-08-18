"""Имя слепка — контракт между backup.sh и peer-sync.sh (спринт 142).

Один скрипт имя СОСТАВЛЯЕТ, другой РАЗБИРАЕТ, и живут они в разных
файлах. Разойдись они — ничего не упадёт: peer-sync просто не найдёт
соседей, скажет «в облаке только мои» и выйдет с нулём. Обмен встанет
молча, и обе машины будут учиться каждая на своей половине датасета,
пока кто-нибудь случайно не сверит счётчики.

Поэтому здесь проверяется именно СВЯЗКА: имя, собранное настоящим кодом
backup.sh, разбирается настоящим выражением из peer-sync.sh.
"""
import re
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
BACKUP = SCRIPTS / "backup.sh"
PEER = SCRIPTS / "peer-sync.sh"


def make_name(label: str) -> str:
    """Собрать имя слепка кодом backup.sh, а не его пересказом."""
    src = BACKUP.read_text(encoding="utf-8")
    m = re.search(r"^HOST_LABEL=\$\(printf.*?\)$", src, re.M | re.S)
    assert m, "в backup.sh не найдено вычисление HOST_LABEL"
    out = re.search(r'^out="\$BACKUP_DIR/(manta-dataset-.*?)"$', src, re.M)
    assert out, "в backup.sh не найдено имя слепка"
    script = f'{m.group(0)}\nprintf "%s" "{out.group(1)}"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"MANTA_HOST_LABEL": label, "BACKUP_DIR": "",
                            "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return r.stdout


def parse_label(name: str) -> str:
    """Вынуть метку из имени выражением из peer-sync.sh."""
    src = PEER.read_text(encoding="utf-8")
    m = re.search(r"sed -n '(s/\^manta-dataset-.*?/p)'", src)
    assert m, "в peer-sync.sh не найдено разбирающее выражение"
    r = subprocess.run(["sed", "-n", m.group(1)], input=name,
                       capture_output=True, text=True)
    return r.stdout.strip()


def test_name_carries_the_label():
    """Без метки в имени обе машины пишут неразличимые файлы."""
    assert "pc1" in make_name("pc1")


def test_round_trip_simple_label():
    assert parse_label(make_name("pc1")) == "pc1"


def test_round_trip_label_with_dashes():
    """Метка с дефисами переживает и сборку, и разбор.

    «wsl-home-pc» — совершенно обычное имя машины, а разбор по первому
    дефису вернул бы «wsl»: машина не узнала бы собственный слепок и
    принялась бы вливать его себе.
    """
    assert parse_label(make_name("wsl-home-pc")) == "wsl-home-pc"


def test_unsafe_label_is_sanitized():
    """Пробелы и слэши в имени машины не должны попадать в путь.

    Метка уходит в путь rclone и в маску find. Слэш увёл бы слепок в
    несуществующий подкаталог, пробел разорвал бы аргумент — и то и
    другое проявилось бы как «бэкап не сохранился» без внятной причины.
    """
    name = make_name("моя машина/DESKTOP 1")
    assert " " not in name and "/" not in name
    assert parse_label(name), "после санитайзинга метка всё ещё разбирается"


def test_timestamp_is_sortable():
    """Дата в имени сортируется лексикографически.

    peer-sync выбирает самый свежий слепок соседа обычным sort. Смени
    формат на «18-08-2026» — и «самым свежим» станет тот, что начинается
    с большей цифры дня, то есть обмен подливал бы старьё.
    """
    name = make_name("pc1")
    stamp = re.search(r"(\d{8}T\d{4})\.tar$", name)
    assert stamp, f"дата в имени не в сортируемом виде: {name}"
