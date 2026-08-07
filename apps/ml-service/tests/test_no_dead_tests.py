"""Страж мёртвых тестов во всём монорепо (спринт 132).

Найдено при ревизии трека F. Скриптовая правка спринтов 60–66 вклинила
хелпер `_pad` между сигнатурой `test_autotrain_thresholds` и её телом:

    def test_autotrain_thresholds(monkeypatch, tmp_path):
        \"\"\"...\"\"\"
        from training import auto
    from training.dataset import FEATURES as _FEATURES


    def _pad(row): ...
        return row + ...
        # ← сюда уехало ВСЁ тело теста

Тело превратилось в недостижимый код после `return` внутри `_pad`, а сам
тест сжался до докстринга и импорта. Он проходил семьдесят спринтов и не
проверял ничего — а сторожил пороги АВТОМАТИЧЕСКОГО переобучения модели.
Мутация подтвердила: с `min_total`, замененным на ноль, тест оставался
зелёным.

Ни pytest, ни ruff такое не ловят: синтаксис корректен, имена определены,
предупреждений нет. Ловится только разбором дерева — что здесь и делается.

Проверка живёт в ml-service, а не в libs, по прозаической причине: CI
гоняет только `apps/<сервис>/tests`, и в libs она бы не запускалась.
Сканирует при этом ВЕСЬ монорепо.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
SKIP_DIRS = {"node_modules", ".git", "build", "__pycache__", "gen"}


def _test_files() -> list[pathlib.Path]:
    return [p for p in REPO.rglob("test_*.py")
            if not SKIP_DIRS & set(p.parts)]


def test_repo_has_test_files_to_check():
    """Если сканер вдруг перестанет что-либо находить, все проверки ниже
    станут пустыми и зелёными — ровно тот же отказ, который они ловят."""
    assert len(_test_files()) > 10


def test_no_unreachable_code_in_any_test_module():
    """Код после return/raise в теле функции — почти всегда результат
    неудачной автоматической правки, а не замысел."""
    dead = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in node.body[:-1]:
                if isinstance(stmt, (ast.Return, ast.Raise)):
                    rel = path.relative_to(REPO)
                    dead.append(f"{rel}:{stmt.lineno} в {node.name}()")
                    break
    assert not dead, "недостижимый код: " + "; ".join(dead)


def test_no_test_function_is_only_a_docstring():
    """Тест из одного докстринга ничего не проверяет.

    Так выглядит функция, у которой тело уехало в соседнюю: снаружи она
    неотличима от рабочей и всегда зелёная.
    """
    empty = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant):
                empty.append(f"{path.relative_to(REPO)}:{node.lineno} "
                             f"{node.name}()")
    assert not empty, "тест без тела: " + "; ".join(empty)
