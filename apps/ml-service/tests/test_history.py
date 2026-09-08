"""Журнал решений гейта (спринт 202).

ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ. Не запись как таковая, а два свойства, каждое из
которых легко нарушить и трудно заметить:

  * журнал НЕ МЕШАЕТ промоушену — недоступная база не отменяет продвижение
    хорошей модели;
  * журнал записывает РЕШЕНИЕ, а не своё мнение о нём — числа берутся те
    самые, по которым продвигали, иначе разбор храповика опирался бы на
    величины, которых гейт не видел.

Живой Postgres здесь не нужен: строка собирается чистой функцией, а сам
SQL проверяется прогоном PREPARE на настоящей схеме
(scripts/tests/test_sql_prepares.py).
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from training.history import UPSERT_SQL, record, row  # noqa: E402

ARTIFACT = {
    "metrics": {"brier": 0.1572, "brier_early": 0.1999},
    "dataset": {"matches": 3297, "max_match_id": 8985733383},
}
HOLDOUTS = [
    {"kind": "benchmark_pro", "n_matches": 666, "brier_new": 0.1572,
     "brier_prod": 0.1565, "delta": 0.0007, "sigma": 0.0009, "ok": True},
    {"kind": "valid", "n_matches": 526, "brier_new": 0.1383,
     "brier_prod": 0.1332, "delta": 0.0051, "sigma": 0.0011, "ok": False},
]


def test_the_row_carries_both_holdouts_not_just_the_deciding_one():
    """ГЛАВНОЕ: записываются ВСЕ оценки, а не только решающая.

    Живой случай 07.09 12:33: модель продвинулась, потому что про-эталон
    прошёл (Δ+0.0007), при валидации хуже почти на 5σ. Запиши мы только
    решающую — из истории следовало бы, что версия была хороша, и
    расхождение между holdout'ами (G2 в роадмапе) осталось бы невидимым
    ровно так же, как оно было невидимо до сих пор.
    """
    got = row("win_probability", "0.9.0", True, "текст", ARTIFACT, HOLDOUTS)
    kinds = [h["kind"] for h in json.loads(got["holdouts"])]
    assert kinds == ["benchmark_pro", "valid"], kinds


def test_the_prod_score_is_recorded_and_makes_the_ratchet_measurable():
    """Brier ПРОДА на том же holdout — то, ради чего таблица заведена.

    Храповик виден только по этой величине: prod съехал 0.1553 → 0.1578
    за сутки четырьмя «незначимыми» шагами. Без brier_prod в истории
    останется лишь качество кандидатов, а вопрос «насколько сдвинулась
    планка» снова придётся собирать по сообщениям в чате.
    """
    holdouts = json.loads(
        row("win_probability", "0.9.0", True, "", ARTIFACT, HOLDOUTS)["holdouts"])
    assert holdouts[0]["brier_prod"] == 0.1565
    assert holdouts[0]["delta"] == 0.0007


def test_a_rejection_is_recorded_too():
    """Отклонённые версии пишутся наравне с продвинутыми.

    Серия отказов подряд — это диагноз (обучение сломалось либо планка
    ушла), и он читается только по истории. Хранить одни успехи значит
    завести журнал, из которого следует, что всё всегда хорошо.
    """
    got = row("win_probability", "0.9.1", False, "значимо хуже prod",
              ARTIFACT, HOLDOUTS)
    assert got["promoted"] is False
    assert got["reason"] == "значимо хуже prod"


def test_the_dataset_size_comes_from_the_artifact_not_recomputed():
    """Размер датасета берётся из артефакта, а не считается заново.

    Журнал обязан записать то, НА ЧЁМ решали. Посчитай он сам — число
    разошлось бы с тем, что видел гейт (данные растут между обучением и
    записью), и история описывала бы не тот прогон.
    """
    assert row("m", "v", True, "", ARTIFACT, [])["dataset_matches"] == 3297


def test_missing_fields_do_not_break_the_row():
    """Артефакт без метрик и датасета не роняет запись.

    Первая версия модели, синтетический прогон, старый артефакт — у всех
    полей может не быть. Падение здесь означало бы, что журнал уронил
    промоушен, то есть ровно то, чего он делать не должен.
    """
    got = row("m", "v", True, "", {}, [])
    assert got["dataset_matches"] == 0
    assert json.loads(got["metrics"]) == {}
    assert json.loads(got["holdouts"]) == []


def test_every_named_parameter_of_the_sql_is_supplied():
    """Ключи строки совпадают с параметрами UPSERT.

    Разъезд здесь не падает при разработке: psycopg сообщит о
    недостающем параметре только на живой базе, то есть в production.
    Та же проверка, что у карточки матча в спринте 192.
    """
    named = {p.split(")")[0] for p in UPSERT_SQL.split("%(")[1:]}
    assert named == set(row("m", "v", True, "", ARTIFACT, HOLDOUTS))


def test_an_unreachable_database_does_not_stop_the_promotion():
    """ГЛАВНОЕ ПРО НАДЁЖНОСТЬ: недоступная база не роняет продвижение.

    Модель — продукт, журнал — её дневник. Обратный порядок приоритетов
    означал бы, что мелкая беда со вспомогательной таблицей стоит
    пользователю модели. Ровно по этому соображению карточка матча
    пишется ПОСЛЕ отчёта и не вместо него.
    """
    ok = record("win_probability", "0.9.0", True, "текст", ARTIFACT, HOLDOUTS,
                dsn="postgresql://нет:нет@127.0.0.1:1/несуществующая")
    assert ok is False, "запись сообщила об успехе при недоступной базе"
