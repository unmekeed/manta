"""Очередь на соли и хранение солей — на ЖИВОМ Postgres (спринт 171).

Смысл наполнителя целиком в двух запросах: кого спрашивать и как хранить
ответ. Фейковый курсор проверял бы сам себя — он возвращает то, что в него
положили, и любая мутация предиката проходит мимо.

ЧТО ЗДЕСЬ ДОРОГО ОШИБИТЬСЯ.

Соль — это fixed32 БЕЗ знака, до 4 294 967 295. В INT (до 2 147 483 647)
не помещается половина возможных значений, и переполнение не падает
красиво: Postgres откажет, наполнитель посчитает матч спрошенным, а соли
у него не будет. Поэтому колонка BIGINT, и это проверяется настоящим
большим числом, а не «на глаз».

Выбор кандидатов важнее, чем кажется. Спросить повторно за уже добытую
соль — значит потратить единицу СУТОЧНОГО бюджета, которого 200–400 на
аккаунт. Двадцать таких промахов в сутки — это десятая часть всей
добычи, и заметить их нечем: наполнитель отработает «успешно».

Запуск:
    ./scripts/sql-test.sh tests/test_replay_salts_sql.py
"""
import os
import pathlib

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("MANTA_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен MANTA_TEST_DSN на одноразовую базу")

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "migrations" / "postgres"
GC_SALTS = ROOT / "scripts" / "gc-node" / "gc-salts.mjs"

SCHEMA = "replay_salts_sql_test"

# Максимальная соль: fixed32 без знака. Она же — проверка того, что
# колонка не INT.
MAX_SALT = 4294967295


def _sql_from_script(marker: str) -> str:
    """Запрос из САМОГО наполнителя, а не его пересказ.

    Копия в тесте проверяла бы копию: разъедься она со скриптом, тест
    остался бы зелёным на запросе, который нигде не выполняется.
    """
    src = GC_SALTS.read_text(encoding="utf-8")
    body = src.split(f"const {marker} = `")[1].split("`;")[0]
    return body.replace("$1", "%s").replace("$2", "%s").replace("$3", "%s")


@pytest.fixture()
def db():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path = {SCHEMA}")
        # Схема из НАСТОЯЩИХ миграций: своя копия проверяла бы сама себя.
        for name in ("002_outbox.sql", "008_replay_candidates.sql",
                     "009_candidate_truth.sql", "011_api_budget.sql",
                     "012_collected_has_replay.sql", "013_parked_replays.sql",
                     "014_replay_salts.sql"):
            cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        cur.execute(f"SET search_path = {SCHEMA}")
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def collected(db, match_id, has_replay=False):
    with db.cursor() as cur:
        cur.execute("INSERT INTO CollectedMatches"
                    " (match_id, source_name, replay_url, has_replay)"
                    " VALUES (%s, 'timeline', 'json:x', %s)",
                    (match_id, has_replay))


def candidate(db, match_id, state="new"):
    with db.cursor() as cur:
        cur.execute("INSERT INTO ReplayCandidates"
                    " (match_id, match_seq_num, state) VALUES (%s, %s, %s)",
                    (match_id, match_id, state))


def salt(db, match_id, cluster=271, value=12345):
    with db.cursor() as cur:
        cur.execute(_sql_from_script("SAVE_SQL"), (match_id, cluster, value))


def pick(db, limit=10):
    with db.cursor() as cur:
        cur.execute(_sql_from_script("PICK_SQL"), (limit,))
        return [r[0] for r in cur.fetchall()]


# -- хранение ------------------------------------------------------------------

def test_the_largest_possible_salt_fits(db):
    """ГЛАВНОЕ про схему: соль беззнаковая, до 4.29 млрд.

    INT вместил бы только половину диапазона. Отказ при вставке выглядел
    бы как «GC отдал ерунду», а не как наша ошибка в типе — и искали бы
    её у Valve.
    """
    salt(db, 100, value=MAX_SALT)
    with db.cursor() as cur:
        cur.execute("SELECT salt FROM ReplaySalts WHERE match_id = 100")
        assert cur.fetchone()[0] == MAX_SALT


def test_saving_twice_is_harmless(db):
    """Повторная запись не падает и не плодит строк.

    Наполнитель ходит по расписанию и может встретить тот же матч дважды
    (например, после перезапуска). Падение здесь остановило бы всю
    порцию из-за одного матча.
    """
    salt(db, 101, value=7)
    salt(db, 101, value=7)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM ReplaySalts WHERE match_id = 101")
        assert cur.fetchone()[0] == 1


# -- кого спрашивать -----------------------------------------------------------

def test_matches_without_replay_are_asked(db):
    """Матч известен, реплея нет, соли нет — спрашиваем."""
    collected(db, 200, has_replay=False)
    assert pick(db) == [200]


def test_matches_with_a_replay_are_not_asked(db):
    """Реплей уже есть — соль не нужна.

    Это ровно та единица бюджета, которую нельзя тратить впустую: их
    200–400 в сутки на аккаунт.
    """
    collected(db, 201, has_replay=True)
    assert pick(db) == []


def test_matches_with_a_known_salt_are_not_asked_again(db):
    """Соль уже добыта — второй раз не спрашиваем.

    Без этого условия наполнитель каждую ночь заново спрашивал бы одни и
    те же свежие матчи, а до новых не доходил бы никогда: список
    отсортирован по убыванию, и голова у него всегда одна и та же.
    """
    collected(db, 202, has_replay=False)
    salt(db, 202)
    assert pick(db) == []


def test_new_candidates_are_asked_too(db):
    """Очередь кандидатов — тоже источник.

    Кандидаты добыты квотой OpenDota на отбор; не спросить у них соль
    значит выбросить эту работу.
    """
    candidate(db, 203, state="new")
    assert pick(db) == [203]


def test_taken_candidates_are_not_asked(db):
    """Кандидат, уже отданный коллектору, не наш.

    Его скачиванием занят реплейный путь, и соль он добудет сам —
    спрашивать её параллельно значит тратить бюджет дважды.
    """
    candidate(db, 204, state="taken")
    assert pick(db) == []


def test_fresh_matches_go_first(db):
    """Свежие вперёд — и это не вкусовщина.

    Valve хранит реплеи около двух недель. Соль к старому матчу — соль к
    файлу, которого уже нет, то есть потраченная единица бюджета в обмен
    на строку, годную только в отчёт.
    """
    for mid in (300, 500, 400):
        collected(db, mid)
    assert pick(db) == [500, 400, 300]


def test_the_limit_is_respected(db):
    """Порция ограничена: бюджет суточный, и брать его залпом нельзя.

    Замеры показали накопительный лимит — первые двести уходят подряд, а
    дальше по капле. Расписание с маленькими порциями расходует его
    ровно; прогон без предела выбрал бы всё за раз.
    """
    for mid in range(600, 610):
        collected(db, mid)
    assert len(pick(db, limit=3)) == 3


def test_both_sources_are_merged_without_duplicates(db):
    """Матч в обоих источниках спрашивается ОДИН раз.

    UNION здесь не косметика: UNION ALL дал бы дубль, наполнитель спросил
    бы дважды и потратил две единицы бюджета на одну соль.
    """
    collected(db, 700, has_replay=False)
    candidate(db, 700, state="new")
    assert pick(db) == [700]


# -- чтение солей коллектором (спринт 172) -------------------------------------

def test_the_store_builds_the_url_from_what_gc_gave(db):
    """Адрес собирается из кластера и соли, взятых из базы.

    Ошибка здесь даёт 404, НЕОТЛИЧИМЫЙ от «соль неверна»: рабочий
    механизм выглядел бы сломанным, и чинили бы не то.
    """
    from collector.salts import SaltStore
    salt(db, 800, cluster=271, value=MAX_SALT)
    got = SaltStore(db).urls_for([800])
    assert got == {
        800: f"http://replay271.valve.net/570/800_{MAX_SALT}.dem.bz2"}


def test_the_store_returns_nothing_for_matches_without_a_salt(db):
    """Нет соли — нет ключа, а не пустая строка и не чужой адрес.

    Спутать «соли нет» с солью соседнего матча значило бы скачать чужой
    файл и записать его как этот матч.
    """
    from collector.salts import SaltStore
    salt(db, 801, cluster=271, value=5)
    got = SaltStore(db).urls_for([801, 802])
    assert set(got) == {801}


def test_the_store_asks_once_for_the_whole_batch(db):
    """Вся порция — одним запросом.

    Цикл кандидатов берёт десятки за проход; обращение к базе на каждый
    матч ради одного целого числа — трата на пустом месте.
    """
    from collector.salts import SaltStore
    for mid in range(810, 820):
        salt(db, mid, cluster=1, value=mid)
    got = SaltStore(db).urls_for(list(range(810, 820)))
    assert len(got) == 10


def test_an_empty_batch_does_not_touch_the_database(db):
    """Пустая порция не ходит в базу вовсе.

    Цикл с пустой очередью — штатное состояние (например, ночью), и
    запрос `WHERE match_id = ANY('{}')` в нём был бы чистым шумом.
    """
    from collector.salts import SaltStore

    class Boom:
        def cursor(self):
            raise AssertionError("пустая порция полезла в базу")

    assert SaltStore(Boom()).urls_for([]) == {}


# -- учёт бюджета GC (спринт 173) ----------------------------------------------
#
# Наполнитель считает СВОЙ расход в той же таблице ApiBudget, что и
# OpenDota, — у неё для этого есть колонка api. Ошибиться здесь можно
# ровно двумя способами, и оба тихие: занизить свой расход (тогда потолок
# не потолок) или попасть в чужой счёт (тогда коллекторы решат, что квота
# OpenDota выбрана, и остановятся при полной квоте).

def spend(db, n, source="gc-salts", api="steam-gc"):
    with db.cursor() as cur:
        cur.execute(_sql_from_script("SPEND_SQL"), (api, source, n))


def used(db, api="steam-gc"):
    with db.cursor() as cur:
        cur.execute(_sql_from_script("USED_SQL"), (api,))
        return cur.fetchone()[0]


def test_spending_accumulates_within_the_day(db):
    """Второй прогон прибавляет, а не заменяет.

    Замена выглядела бы как «за сутки 8 из 400» после сорока прогонов —
    потолок перестал бы быть потолком, а расход мы бы недосчитали ровно
    настолько, насколько усердно работали.
    """
    spend(db, 8)
    spend(db, 5)
    assert used(db) == 13


def test_gc_spending_does_not_touch_the_opendota_budget(db):
    """Чужой счёт не трогаем — иначе остановим сбор при полной квоте.

    Все запросы бюджета фильтруют по api. Попади наш расход в строки
    OpenDota — коллекторы решили бы, что 2000 в сутки выбраны, и
    замолчали бы при нетронутой квоте.
    """
    spend(db, 300)
    assert used(db, api="opendota") == 0


def test_all_sources_of_one_api_share_the_ceiling(db):
    """Потолок общий на api, а не на процесс.

    Бюджет живёт у АККАУНТА Steam. Считать его по каждому процессу
    отдельно значило бы разрешить двум наполнителям выбрать двойную
    норму — ровно так квота OpenDota однажды ушла в минус.
    """
    spend(db, 100, source="gc-salts")
    spend(db, 50, source="gc-salts-2")
    assert used(db) == 150


# -- очередь на скачивание по добытой соли (спринт 179) ------------------------
#
# ЖИВОЙ СЛУЧАЙ 2026-09-02: солей 16, кандидатов 0, скачано ноль. Соли
# читались только по списку кандидатов, а на VPS этот список пуст — его
# наполняет `ranks scan`, которого нет ни в одном расписании (спринт 154).
# Добыча шла исправно, потребителя у добытого не было.

def parked(db, match_id, url="http://old/1.bz2"):
    with db.cursor() as cur:
        cur.execute("INSERT INTO ParkedReplays (match_id, replay_url, host,"
                    " reason) VALUES (%s, %s, 'h', 'r')", (match_id, url))


def wanted(db, limit=10):
    from collector.salts import SaltStore
    return SaltStore(db).wanted(limit)


def test_a_salt_without_a_replay_is_queued(db):
    """ГЛАВНОЕ: соль есть, реплея нет — качаем.

    Ни очередь кандидатов, ни OpenDota, ни квота здесь не участвуют:
    всё нужное уже лежит в базе.
    """
    collected(db, 900, has_replay=False)
    salt(db, 900, cluster=182, value=77)
    assert wanted(db) == [
        (900, "http://replay182.valve.net/570/900_77.dem.bz2")]


def test_a_collected_replay_leaves_the_queue_by_itself(db):
    """Скачанный матч исчезает сам, без второй отметки «сделано».

    Вторая отметка — это второй способ с ней разойтись: матч считался бы
    нужным после успеха или ненужным до него.
    """
    collected(db, 901, has_replay=True)
    salt(db, 901)
    assert wanted(db) == []


def test_a_match_we_never_saw_is_not_queued(db):
    """Соль без матча в CollectedMatches не качается.

    Скачать реплей матча, которого мы не собирали, значит записать его в
    витрину в обход отбора — то есть тихо расширить датасет мусором.

    Тонкость, замеченная мутацией: замена JOIN на LEFT JOIN этот тест НЕ
    ломает — `WHERE NOT c.has_replay` отсекает NULL-строки сам, по
    трёхзначной логике SQL. Мутация эквивалентна, и убивать её нечем.
    JOIN оставлен потому, что выражает намерение прямо, а не полагается
    на побочное свойство условия в WHERE: убери кто-нибудь это условие —
    и LEFT JOIN начал бы отдавать соли без матчей.
    """
    salt(db, 902)
    assert wanted(db) == []


def test_parked_matches_are_left_to_their_own_source(db):
    """Припаркованные не берутся: ими занят ParkedSource.

    Взять их обоими источниками значит качать 58 МиБ дважды.
    """
    collected(db, 903, has_replay=False)
    salt(db, 903)
    parked(db, 903)
    assert wanted(db) == []


def test_unreachable_chinese_clusters_are_not_queued(db):
    """Кластеры 4xx не качаются.

    До них нет маршрута ни с VPS, ни из дома (спринт 153, 141 матч
    припаркован именно оттуда). Каждая попытка стоила бы полного
    таймаута ради заведомо известного ответа. Соль при этом хранится —
    знать, что матч недостижим, полезно.
    """
    collected(db, 904, has_replay=False)
    salt(db, 904, cluster=414)
    assert wanted(db) == []
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM ReplaySalts WHERE match_id = 904")
        assert cur.fetchone()[0] == 1, "соль недостижимого матча стёрлась"


def test_fresh_matches_are_downloaded_first(db):
    """Свежие вперёд: Valve держит реплеи около двух недель."""
    for mid in (910, 930, 920):
        collected(db, mid, has_replay=False)
        salt(db, mid)
    assert [m for m, _ in wanted(db)] == [930, 920, 910]


def test_the_download_limit_is_respected(db):
    """Порция ограничена: реплей весит 58 МиБ, и канал — узкое место."""
    for mid in range(940, 950):
        collected(db, mid, has_replay=False)
        salt(db, mid)
    assert len(wanted(db, limit=3)) == 3
