"""Пустая таблица в архиве не должна ронять импорт (инцидент 2026-08-01).

Слепок от 2026-07-27 содержал MatchDraft, но пустой: трек F наполнил её
позже. ClickHouse на пустом Native отвечает NO_DATA_TO_INSERT, скрипт
падал по `set -e`, и импорт обрывался ПОСЕРЕДИНЕ — витрины успевали
влиться, сырьё и таблицы Postgres терялись. Пользователь при этом видел
успешно перенесённые первые таблицы и не сразу понимал, что данных нет.
"""
import gzip
import re
import subprocess
import tempfile
from pathlib import Path

SYNC = Path(__file__).resolve().parents[1] / "dataset-sync.sh"


def _is_empty_dump() -> str:
    src = SYNC.read_text(encoding="utf-8")
    m = re.search(r"^is_empty_dump\(\) \{.*?^\}", src, re.M | re.S)
    assert m, "функция is_empty_dump не найдена в dataset-sync.sh"
    return m.group(0)


def _check(path: Path) -> bool:
    """True, если скрипт считает дамп пустым."""
    harness = f'{_is_empty_dump()}\nis_empty_dump "{path}" && echo EMPTY || echo DATA'
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return "EMPTY" in r.stdout


def test_empty_gzip_is_detected():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "MatchDraft.native.gz"
        p.write_bytes(gzip.compress(b""))
        assert _check(p)


def test_non_empty_gzip_is_not_skipped():
    """Обратная сторона: дамп с данными обязан импортироваться, иначе
    «защита» тихо выбросит половину датасета."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "MatchTimelineFeatures.native.gz"
        p.write_bytes(gzip.compress(b"\x01\x02binary native dump"))
        assert not _check(p)


def test_import_skips_empty_tables_in_loop():
    """Проверка на уровне текста скрипта: обе петли импорта (Replacing и
    Raw) вызывают is_empty_dump. Пропущенная петля — тот же обрыв."""
    src = SYNC.read_text(encoding="utf-8")
    body = src[src.index("import_dataset()"):]
    assert body.count("is_empty_dump") == 2, (
        "обе петли импорта должны пропускать пустые дампы")
