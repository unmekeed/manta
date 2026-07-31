"""Исполняемые скрипты не должны быть пустыми или несинтаксичными.

Инцидент 2026-07-31: scripts/dataset-sync.sh уехал в коммит обнулённым
(0 байт). Пустой bash-скрипт завершается с кодом 0 — то есть
`manta up` вызвал импорт бэкапа, получил «успех» и продолжил с пустой
витриной. Отказ был полностью молчаливым: ни ошибки, ни предупреждения.

Проверка дешёвая, а класс ошибок закрывает целиком.
"""
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

# Минимальный разумный размер: заголовок с shebang плюс хоть какая-то
# логика. Настоящие скрипты проекта — от 40 строк.
MIN_BYTES = 200


def _shell_scripts() -> list[Path]:
    out = [p for p in SCRIPTS.glob("*.sh")]
    out.append(SCRIPTS / "manta")          # диспетчер без расширения
    return sorted(p for p in out if p.exists())


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_script_is_not_empty(script: Path):
    size = script.stat().st_size
    assert size >= MIN_BYTES, (
        f"{script.name} подозрительно мал ({size} байт). Пустой скрипт "
        f"возвращает 0 и его отказ ничем себя не проявляет")


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_script_parses(script: Path):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True,
                       text=True)
    assert r.returncode == 0, f"{script.name}: {r.stderr}"


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_script_is_executable(script: Path):
    assert script.stat().st_mode & 0o111, (
        f"{script.name} не исполняем — make-цели вызовут его напрямую")
