"""Ежедневный снимок диагностики (спринт 112).

Мотив: самые дорогие ошибки проекта были не мгновенными, а постепенными
— темп сбора сползал неделю, про-эталон замёрз на двое суток, покрытие
фич падало от патча к патчу. Всё это видно только в сравнении с
прошлым, а прошлого не оставалось: вывод команд жил в терминале до
следующей прокрутки.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "daily-report.sh"
ROOT = SCRIPT.parents[1]


def _run(tmp_path, **env):
    e = dict(os.environ)
    e.update({"MANTA_REPORT_DIR": str(tmp_path),
              "MANTA_TRAIN_ENV": str(tmp_path / "нет.env"),
              "SECTION_TIMEOUT_S": "20"})
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                          text=True, env=e, cwd=str(ROOT), timeout=300)


def test_report_written_even_when_everything_is_down(tmp_path):
    """Главное свойство. Диагностику смотрят ровно тогда, когда что-то
    сломано: ClickHouse лежит, модель не обучена, докера нет. Скрипт
    обязан дойти до конца и записать, ЧТО именно не ответило.

    Здесь ни ClickHouse, ни докера нет вовсе — то есть худший случай.
    """
    r = _run(tmp_path)
    files = list(tmp_path.glob("manta-*.log"))
    assert len(files) == 1, f"отчёт не создан: {r.stdout}\n{r.stderr}"
    text = files[0].read_text(encoding="utf-8")
    # Все четыре раздела должны присутствовать, пусть и с ошибками внутри.
    for title in ("doctor", "collect-report", "ml-status", "ml-audit"):
        assert title in text, f"раздел «{title}» потерян"
    assert "ИТОГ" in text, "отчёт оборвался, не дойдя до конца"
    assert r.returncode == 0


def test_no_set_e(tmp_path):
    """`set -e` здесь запрещён: он обрывает отчёт на первом же сломанном
    разделе — то есть молчит именно в том случае, ради которого отчёт и
    нужен. Та же причина, что в collect-report.sh."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "set -uo pipefail" in src
    assert "set -euo pipefail" not in src


def test_rotation_removes_only_old_files(tmp_path):
    """Ротация 30 дней. Свежие файлы обязаны уцелеть — иначе история, ради
    которой всё делалось, исчезает при первом же запуске."""
    old = tmp_path / "manta-2020-01-01.log"
    old.write_text("древний", encoding="utf-8")
    os.utime(old, (0, 0))                       # 1970 год — заведомо старый
    fresh = tmp_path / "manta-2026-08-01.log"
    fresh.write_text("свежий", encoding="utf-8")

    _run(tmp_path, REPORT_KEEP_DAYS="30")
    assert not old.exists(), "старый отчёт не удалён"
    assert fresh.exists(), "удалён отчёт, который должен был остаться"


def test_rotation_runs_after_writing(tmp_path):
    """Порядок как в backup.sh: сначала пишем новый, потом чистим старые.
    Наоборот — и сбой записи оставил бы вообще без истории."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert src.index('} >"$out"') < src.index("-print -delete")


def test_one_file_per_day(tmp_path):
    """Повторный запуск в тот же день переписывает файл, а не плодит
    второй: иначе `diff` вчера/сегодня перестаёт работать, а каталог
    растёт числом запусков, а не дней."""
    _run(tmp_path)
    _run(tmp_path)
    assert len(list(tmp_path.glob("manta-*.log"))) == 1


def test_header_records_machine_and_commit(tmp_path):
    """Отчёты с двух машин попадают в один каталог при синхронизации, и
    без имени машины их не различить. Коммит нужен, чтобы «поведение
    изменилось» можно было связать с правкой кода."""
    _run(tmp_path)
    text = next(tmp_path.glob("manta-*.log")).read_text(encoding="utf-8")
    assert "машина:" in text and "коммит:" in text
    # Файл обязан быть валидным UTF-8 целиком. Первая версия резала тему
    # коммита через `head -c 60` — то есть по БАЙТАМ, разрубая
    # кириллический символ пополам; отчёт становился нечитаемым, а
    # сообщения у нас на русском, так что ломалось это каждый раз.
    files = list(tmp_path.glob("manta-*.log"))
    for f in files:
        f.read_bytes().decode("utf-8")
    assert "COLLECTOR_SHARD_COUNT" in text, (
        "без номера шарда два отчёта неотличимы по охвату сбора")


def test_scheduler_task_registered():
    """Ручной запуск раз в неделю — это не ежедневный отчёт. Задача
    планировщика должна ставиться тем же скриптом, что Manta-Backup."""
    ps = (Path(__file__).resolve().parents[1] / "autostart-install.ps1"
          ).read_text(encoding="utf-8")
    assert "Manta-Report" in ps
    assert "daily-report.sh" in ps
    # И сниматься при -Uninstall: осиротевшая задача переживёт удаление
    # проекта и будет молча падать каждый день.
    i = ps.index("if ($Uninstall)")
    assert "Manta-Report" in ps[i:i + 400]


@pytest.mark.parametrize("target", ["daily-report"])
def test_make_target_exists(target):
    src = (Path(__file__).resolve().parents[2] / "Makefile"
           ).read_text(encoding="utf-8")
    assert f"\n{target}:" in src
