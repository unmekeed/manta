"""Тесты SQL-семантики точности отбора (спринт 130.2).

Почему отдельный файл с настоящим Postgres, а не фейком.

Смысл метрики целиком живёт в SQL: «все immortal» — это строгое
равенство, «мусор» — это МЕНЬШЕ ПОЛОВИНЫ, а «доля immortal» — среднее по
матчам, а не по игрокам. Фейковый курсор такие вещи не проверяет: он
возвращает то, что в него положили, и любая мутация предиката проходит
мимо. Первая версия метрики именно так и уехала в main неправильной —
она считала матч удачным при среднем ранге >= 80, у Immortal нет звёзд
(ровно 80), Divine 5 — это 75, и матч из восьми имморталов и одного
дивайна давал 79.4 -> 79 и шёл в брак. Живые данные показали 79.3%
«точности» при почти полном отсутствии настоящего мусора.

Запуск:
    make candidates-sql-test        # локальный PG из docker-compose
или вручную:
    MANTA_TEST_DSN=postgresql://... pytest tests/test_candidates_sql.py

Без MANTA_TEST_DSN файл пропускается: в CI постоянного Postgres нет, и
это честно означает «здесь эта проверка не выполнялась», а не «зелено».

Рабочую базу тест не трогает: он живёт в собственной схеме, создаёт и
удаляет только её, а таблицы public не видит вовсе.
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MANTA_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен MANTA_TEST_DSN на одноразовую базу")

MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / \
    "infra" / "migrations" / "postgres"

# Отдельная схема, а не public: если кто-то направит MANTA_TEST_DSN на
# рабочую базу, тест не тронет её таблицы, а упадёт на своей.
SCHEMA = "candidates_sql_test"


@pytest.fixture()
def queue():
    from collector.candidates import CandidateQueue

    db = psycopg.connect(DSN, autocommit=True)
    with db.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        # Схему берём из настоящих миграций, а не из руками написанного
        # CREATE TABLE: иначе тест проверял бы свою копию, а не то, что
        # реально приезжает на прод.
        for name in ("008_replay_candidates.sql", "009_candidate_truth.sql"):
            cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))

    q = CandidateQueue.__new__(CandidateQueue)
    q._db = db
    yield q

    with db.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    db.close()


def _add(queue, match_id, state="done", known=None, immortal=None, avg=None):
    with queue._db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {SCHEMA}.ReplayCandidates"
            "  (match_id, match_seq_num, state,"
            "   true_known_ranks, true_immortal_ranks, true_avg_rank)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (match_id, match_id, state, known, immortal, avg))


def test_all_immortal_requires_every_known_rank(queue):
    """Строгий критерий — именно строгий: 8 из 9 сюда не попадают."""
    _add(queue, 1, known=9, immortal=9, avg=80)
    _add(queue, 2, known=9, immortal=8, avg=79)
    prec = queue.precision()
    assert prec["все immortal"] == 1, prec


def test_one_divine_among_immortals_is_not_garbage(queue):
    """Ровно тот матч, который старая метрика браковала.

    Восемь имморталов и один дивайн: средний ранг 79.4 -> 79, старый
    критерий «avg >= 80» объявлял такой матч промахом правила отбора.
    """
    _add(queue, 1, known=9, immortal=8, avg=79)
    prec = queue.precision()
    assert prec["мусор (immortal < половины)"] == 0, prec
    assert prec["доля immortal, %"] == 89, prec


def test_garbage_is_strictly_less_than_half(queue):
    """Граница проходит по половине, а не по «не все имморталы».

    5 из 9 — большинство, это не мусор; 4 из 9 — меньшинство, это мусор.
    Матч ровно посередине невозможен при нечётном числе известных, а при
    чётном (4 из 8) половина мусором не считается: правило ошиблось бы
    только тогда, когда имморталы в меньшинстве.
    """
    _add(queue, 1, known=9, immortal=5, avg=78)
    _add(queue, 2, known=9, immortal=4, avg=77)
    _add(queue, 3, known=8, immortal=4, avg=77)
    prec = queue.precision()
    assert prec["мусор (immortal < половины)"] == 1, prec


def test_share_is_averaged_per_match_not_pooled_over_players(queue):
    """Доля — средняя по МАТЧАМ, иначе один матч с девятью рангами
    перевесит девять матчей с одним рангом, и метрика начнёт мерить
    полноту данных OpenDota, а не качество отбора.

    Здесь пул по игрокам дал бы 1/10 = 10%, среднее по матчам — 50%.
    """
    _add(queue, 1, known=1, immortal=1, avg=80)
    _add(queue, 2, known=9, immortal=0, avg=60)
    prec = queue.precision()
    assert prec["доля immortal, %"] == 50, prec


def test_unknown_truth_does_not_dilute_the_metric(queue):
    """NULL — это «не смотрели», а не «рангов не было».

    Матчи, скачанные до миграции 009, обязаны выпадать из знаменателя:
    иначе точность падала бы от нашего собственного незнания.
    """
    _add(queue, 1, known=9, immortal=9, avg=80)
    _add(queue, 2)                       # факта нет
    prec = queue.precision()
    assert prec["скачано"] == 2, prec
    assert prec["факт известен"] == 1, prec
    assert prec["доля immortal, %"] == 100, prec


def test_only_downloaded_candidates_are_counted(queue):
    """Точность правила — про скачанное. Ждущий в очереди матч факта
    иметь не может, а если бы имел, он говорил бы о будущем, не о
    результате."""
    _add(queue, 1, state="done", known=9, immortal=9, avg=80)
    _add(queue, 2, state="new", known=9, immortal=0, avg=50)
    prec = queue.precision()
    assert prec["скачано"] == 1, prec
    assert prec["доля immortal, %"] == 100, prec


def test_empty_queue_returns_zeroes_not_none(queue):
    """Пустая таблица не должна ронять отчёт на None в форматировании."""
    assert queue.precision() == {
        "скачано": 0, "факт известен": 0, "все immortal": 0,
        "доля immortal, %": 0, "мусор (immortal < половины)": 0,
        "средний ранг": 0, "рангов на матч": 0}
