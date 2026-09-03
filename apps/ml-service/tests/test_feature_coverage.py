"""Мета-тест: фича без теста не проходит (спринт 137).

Обычные тесты проверяют код, который есть. Этот проверяет, что тесты
ЕСТЬ: каждое имя из FEATURES обязано встретиться хотя бы в одном тестовом
файле монорепозитория. Добавил сорок вторую фичу и не написал ни строчки
про неё — сборка красная.

Зачем это отдельно от голденов. Голден вектора покрывает все фичи, но
он ГЕНЕРИРУЕТСЯ из FEATURES: новая фича попадёт в эталон сама, без
единого утверждения о том, что она считается правильно. Автопокрытие —
это не покрытие, и голден такой пропуск не поймает по построению.

Чего этот тест НЕ делает. Он ищет ПОДСТРОКУ, поэтому упоминание в
комментарии он зачтёт за тест. Заменить человека, который решает, что
именно проверить, он не может и не пытается: его работа — не дать
добавить фичу МОЛЧА. Что число посчитано верно, проверяют голдены рядом.

Фикстуры намеренно не сканируются: golden_vector.json собирается из
FEATURES, и включение фикстур сделало бы мета-тест тождественно зелёным.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs"))

from training.dataset import FEATURES  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
# Файл-исключение: себя мета-тест за покрытие не считает. Иначе он
# зачитывал бы собственный вывод об отсутствии покрытия как покрытие.
SELF = Path(__file__).name


def _test_sources() -> list[Path]:
    return sorted(p for p in REPO.rglob("test_*.py")
                  if ".git" not in p.parts and p.name != SELF)


def test_repo_scan_finds_the_test_suite():
    """Страховка от тихой поломки самого мета-теста.

    Если корень репозитория определён неверно, сканирование вернёт пустой
    список, и следующий тест упадёт с «не покрыто ни одной» — то есть
    громко. А вот если сюда когда-нибудь попадёт `or []`, мета-тест станет
    зелёным навсегда. Проверяем порядок величины явно.
    """
    found = _test_sources()
    assert len(found) > 30, f"найдено тестовых файлов: {len(found)}"


def test_every_model_feature_is_named_in_some_test():
    blob = "\n".join(p.read_text(errors="ignore") for p in _test_sources())
    missing = [f for f in FEATURES if f not in blob]
    assert not missing, (
        "фичи модели, не упомянутые ни в одном тесте:\n"
        + "\n".join(f"  {f}" for f in missing)
        + "\n\nГолден вектора покроет их автоматически, и это не покрытие: "
          "нужен тест, который утверждает, ЧТО фича означает.")


def test_features_list_has_no_duplicates():
    """Дубль в FEATURES — это молча удвоенный вес признака.

    Сборка вектора идёт по именам через словарь, поэтому дубль не падает:
    в вектор дважды попадёт одно и то же число, LightGBM получит два
    одинаковых столбца, и важность признака разделится между ними —
    абляция после этого показывает «фича бесполезна» на обеих половинах.
    """
    dupes = sorted({f for f in FEATURES if list(FEATURES).count(f) > 1})
    assert not dupes, f"дубли в FEATURES: {dupes}"


def test_feature_list_is_parseable_without_importing(module="dataset"):
    """FEATURES должен читаться из файла статически, через ast.

    Так его читает report-generator: свои зависимости ml-service он не
    ставит, поэтому импортировать dataset.py не может и разбирает список
    парсером. Если FEATURES когда-нибудь начнут СОБИРАТЬ в рантайме
    (генератором, конкатенацией, циклом), парсер увидит не список строк,
    а выражение, и вектор в проде разъедется с обучением молча.
    """
    src = (REPO / "apps/ml-service/src/training/dataset.py").read_text()
    tree = ast.parse(src)
    literal = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "FEATURES"
                        for t in node.targets)):
            literal = node.value
    assert isinstance(literal, (ast.List, ast.Tuple)), (
        "FEATURES перестал быть литералом — report-generator читает его "
        "через ast и собранный в рантайме список не увидит")
    names = [e.value for e in literal.elts if isinstance(e, ast.Constant)]
    assert names == list(FEATURES), (
        "статически прочитанный FEATURES не совпал с импортированным")
