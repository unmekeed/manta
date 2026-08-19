"""Форма запросов дедупа — то немногое, что проверяется без Postgres (151).

Смысл правки живёт в SQL, и настоящая проверка — tests/test_dedup_sql.py
на живой базе. Но она пропускается везде, где базы нет (CI, чужая
машина), а «пропущено» и «пройдено» в сводке различают не всегда. Здесь —
то, что можно утверждать по тексту: предикат на месте, и два пути
разрешают конфликт ПО-РАЗНОМУ.

Именно асимметрия и есть суть: реплейный путь ПОВЫШАЕТ строку
(DO UPDATE … has_replay = TRUE), JSON-путь не трогает чужую
(DO NOTHING). Сделай оба одинаковыми — и один из двух сценариев ломается
молча: либо реплей качается каждый цикл заново, либо JSON затирает
отметку разобранного матча.
"""
import ast
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

REPLAY = (SRC / "collector" / "runner.py").read_text(encoding="utf-8")
TIMELINE = (SRC / "collector" / "timeline_runner.py").read_text(encoding="utf-8")


def _body(text: str, name: str) -> str:
    """Тело метода по имени — БЕЗ докстроки и комментариев.

    Вычищать прозу обязательно. Первая версия сравнивала текст как есть, и
    две мутации её пережили: комментарий над INSERT в timeline_runner
    содержит и слова «DO NOTHING», и «FALSE», так что проверка находила их
    даже после того, как из самого запроса они исчезали. Четвёртый случай
    за эту серию, когда собственная проверка спотыкается о пояснение к
    коду: до этого были backup-drill, мигратор и requirements.txt.

    Вторая версия резала докстроку регуляркой по тройным кавычкам — и
    съедала заодно сами SQL-запросы, которые ими же и записаны. Разбор
    через ast снимает оба вопроса разом: комментарии до дерева не
    доходят вовсе, а докстрока — просто первый узел.
    """
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.unparse(s) for s in body)
    raise AssertionError(f"метод {name} не найден — разбор сломан")


def test_the_slicer_finds_real_bodies():
    """Страховка от проверки пустоты: срез действительно что-то берёт."""
    assert "CollectedMatches" in _body(REPLAY, "_is_collected")
    assert "INSERT INTO CollectedMatches" in _body(REPLAY, "_mark_collected")
    assert "INSERT INTO CollectedMatches" in _body(TIMELINE, "_mark_collected")


def test_replay_path_asks_for_has_replay():
    """Реплейный путь спрашивает про РЕПЛЕЙ, а не про факт сбора.

    Уберите предикат — и вернётся ровно та поломка: JSON-матчи снова
    станут дубликатами, PositionSnapshots останется пуст, карт не будет.
    """
    assert "has_replay" in _body(REPLAY, "_is_collected"), (
        "реплейный путь снова считает дубликатом любой собранный матч")


def test_json_path_asks_about_any_collection():
    """А JSON-путь — про любой сбор: реплей даёт всё то же и сверх того."""
    body = _body(TIMELINE, "_is_collected")
    assert "CollectedMatches" in body
    assert "has_replay" not in body, (
        "JSON-путь начал собирать матчи, уже разобранные из реплея")


def test_replay_path_upgrades_the_row():
    body = _body(REPLAY, "_mark_collected")
    assert "DO UPDATE" in body and "has_replay  = TRUE" in body, (
        "без DO UPDATE отметка не поднимется, и матч будет качаться "
        "каждый цикл заново")


def test_json_path_does_not_touch_an_existing_row():
    body = _body(TIMELINE, "_mark_collected")
    assert "DO NOTHING" in body, (
        "JSON-путь затрёт отметку разобранного матча, и реплей скачается "
        "заново")
    assert "FALSE" in body, "JSON-путь обязан ставить has_replay = FALSE явно"
