"""Переразбор матча обязан ЗАМЕЩАТЬ агрегат, а не дополнять (спринт 106).

Миграции 020/021 обещают «переразбор ЗАМЕЩАЕТ строку» — и это верно ровно
до тех пор, пока пересчёт даёт те же значения ключа сортировки. В ключ
входят hero/kind/name (тайминги) и координаты клетки (карты), так что
любая правка справочника предметов, нормализации героев или
классификации создаёт НОВЫЙ ключ: строка ложится РЯДОМ со старой.

Инцидент 2026-08-05: правка справочника предметов оставила 8764
строки-призрака вида `item_49`. Заметить их нечем — агрегат выглядит
полным, просто строк в нём больше, чем было событий.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from extractor.runner import Extractor  # noqa: E402


class FakeCH:
    def __init__(self, existing=0, fail_count=False):
        self.existing = existing
        self.fail_count = fail_count
        self.calls = []

    def select(self, query, params=None):
        self.calls.append(("select", query, params))
        if self.fail_count:
            raise RuntimeError("ClickHouse недоступен")
        return [{"n": self.existing}]

    def execute(self, query, params=None):
        self.calls.append(("execute", query, params))

    def insert_rows(self, table, rows):
        self.calls.append(("insert", table, list(rows)))


def _ex(ch):
    """Экстрактор без конструктора: нужен только метод _replace_rows."""
    ex = Extractor.__new__(Extractor)
    ex.ch = ch
    return ex


def _kinds(ch):
    return [c[0] for c in ch.calls]


def test_existing_rows_are_deleted_before_insert():
    ch = FakeCH(existing=42)
    _ex(ch)._replace_rows("MatchHeroTimings", 7, [{"a": 1}])
    assert _kinds(ch) == ["select", "execute", "insert"], (
        "удаление обязано идти ДО вставки")
    assert "DELETE FROM MatchHeroTimings" in ch.calls[1][1]
    assert ch.calls[1][2] == {"match_id": 7}


def test_delete_is_synchronous():
    """Порядок «удалить, потом вставить» разваливается, если удаление
    применится ПОЗЖЕ вставки: маска по match_id снесёт и только что
    записанные строки. Полагаться на то, что дефолт настройки и завтра
    равен 2, нельзя — задаём явно."""
    ch = FakeCH(existing=1)
    _ex(ch)._replace_rows("MatchMapCells", 7, [{"a": 1}])
    assert "lightweight_deletes_sync = 2" in ch.calls[1][1]


def test_no_delete_when_nothing_to_replace():
    """Обычный путь — первый разбор матча. Удалять нечего, и платить за
    мутацию на каждом матче не нужно."""
    ch = FakeCH(existing=0)
    _ex(ch)._replace_rows("MatchHeroTimings", 7, [{"a": 1}])
    assert _kinds(ch) == ["select", "insert"]


def test_empty_rows_still_clear_old_ones():
    """Матч мог законно лишиться строк — например, все события оказались
    браком. Пропустить удаление значило бы оставить прежний агрегат жить
    вечно под видом актуального."""
    ch = FakeCH(existing=5)
    _ex(ch)._replace_rows("MatchHeroTimings", 7, [])
    assert _kinds(ch) == ["select", "execute", "insert"]


def test_count_failure_does_not_lose_the_match():
    """Не смогли проверить — вставляем как раньше. Дубль хуже пропуска,
    но потеря матча хуже дубля."""
    ch = FakeCH(fail_count=True)
    _ex(ch)._replace_rows("MatchHeroTimings", 7, [{"a": 1}])
    assert _kinds(ch) == ["select", "insert"]


def test_runner_uses_replace_for_both_aggregates():
    """Прямой insert_rows мимо _replace_rows вернул бы призраков, и
    тесты выше этого не увидели бы."""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src" / "extractor" / "runner.py").read_text(encoding="utf-8")
    for table in ("MatchMapCells", "MatchHeroTimings"):
        assert f'self._replace_rows("{table}"' in src
        assert f'self.ch.insert_rows("{table}"' not in src
