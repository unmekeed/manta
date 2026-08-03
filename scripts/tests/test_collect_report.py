"""collect-report.sh — диагностический дамп, и он обязан быть безопасным.

Отчёт запускают именно тогда, когда что-то уже сломалось: приток встал,
причина неизвестна, под рукой одна команда. Два свойства делают его
пригодным для этого момента, и оба легко потерять при правке:

1. Он ТОЛЬКО читает. Скрипт, который в такой момент что-то запишет или
   перезапустит, уничтожит улику — а именно улика и нужна.
2. Он не обрывается на первой ошибке. Половина отчёта полезна; `set -e`
   превратил бы недоступный Postgres в пустой экран вместо разделов про
   ClickHouse, логи и квоты.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "scripts" / "collect-report.sh"
SRC = REPORT.read_text(encoding="utf-8")


def test_exists_and_parses():
    r = subprocess.run(["bash", "-n", str(REPORT)], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("verb", [
    "INSERT", "ALTER", "DROP", "TRUNCATE", "OPTIMIZE", "DELETE FROM",
    "CREATE TABLE",
])
def test_no_mutating_sql(verb: str):
    """Ни одного изменяющего запроса — ни в ClickHouse, ни в Postgres."""
    assert verb not in SRC.upper().replace("COLLECTEDMATCHES", ""), (
        f"в отчёте встретился {verb}: диагностика обязана только читать")


@pytest.mark.parametrize("cmd", ["rm ", "docker compose", "pkill", "kill ",
                                 "systemctl", "dev-recover", "git "])
def test_no_side_effects(cmd: str):
    body = "\n".join(l for l in SRC.splitlines()
                     if not l.strip().startswith("#"))
    assert cmd not in body, (
        f"«{cmd}» меняет состояние машины — отчёт не должен ничего чинить")


def test_does_not_abort_on_first_error():
    """set -u и pipefail нужны, а set -e ломает саму идею отчёта."""
    m = re.search(r"^set (-\S+)", SRC, re.M)
    assert m, "нет строки set в начале скрипта"
    flags = m.group(1)
    assert "e" not in flags.replace("-", ""), (
        "set -e оборвёт отчёт на первом недоступном источнике данных")
    assert "u" in flags


@pytest.mark.parametrize("secret", ["STRATZ_API_TOKEN", "OPENDOTA_API_KEY"])
def test_secrets_are_never_printed(secret: str):
    """Значение секрета не попадает в вывод — только факт его наличия.

    Отчёт пересылают в переписку целиком, в этом весь смысл одной
    команды. Поэтому подстановка секрета разрешена ровно в трёх местах:
    заголовок Authorization реального запроса, проверка `-n` на
    заполненность и форма `${#VAR}` (длина, не содержимое).
    """
    allowed = (f"${{#{secret}}}", f'-n "${{{secret}:-}}"',
               f'[ -n "${secret}" ]', "Authorization: Bearer")
    for line in SRC.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not re.search(r"\$\{?" + secret, line):
            continue
        assert any(a in line for a in allowed), (
            f"значение {secret} может утечь в отчёт: {stripped}")


def test_feature_coverage_is_not_hardcoded():
    """Список фич берётся из system.columns.

    Захардкоженный список молча устаревает: новая миграция добавляет
    колонку, отчёт про неё не знает, и «покрытие 100%» относится к
    старому набору. Именно так пропускают неработающего поставщика фичи.
    """
    assert "system.columns" in SRC
    assert "isNaN" in SRC


def test_sections_are_selectable():
    """Разделы вызываются по отдельности: полный дамп длинный, а во время
    инцидента чаще нужен только темп сбора."""
    for section in ("rate", "features", "logs", "procs", "quota"):
        assert f"want {section}" in SRC, f"раздел {section} не выбирается"


def test_makefile_target():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "collect-report:" in mk
    assert "./scripts/collect-report.sh" in mk


def test_every_collector_log_is_covered():
    """Все шесть логов коллекторов из dev-recover.sh — в отчёте.

    Пропущенный лог означает источник, чью остановку отчёт не заметит.
    """
    recover = (ROOT / "scripts" / "dev-recover.sh").read_text(encoding="utf-8")
    logs = set(re.findall(r'LOG_DIR/([\w-]+)\.log', recover))
    collector_logs = {n for n in logs
                      if n in {"collector", "timeline", "timeline-pro",
                               "stratz", "stratz-pro", "pro-collector"}}
    assert collector_logs, "в dev-recover.sh не нашлось логов коллекторов"
    for name in collector_logs:
        assert name in SRC, f"лог {name}.log не читается отчётом"
