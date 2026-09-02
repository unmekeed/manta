"""Источник «есть соль — качаем» (спринт 179).

Смысл источника — замкнуть путь, который до 2026-09-02 обрывался: соли
добывались, но читались только по списку кандидатов, а на VPS этот список
пуст (его наполняет `ranks scan`, которого нет ни в одном расписании,
спринт 154). Живая проверка показала 16 солей и 0 кандидатов.

Выборка проверяется на ЖИВОМ Postgres (test_replay_salts_sql.py): смысл
её предиката фейковым курсором не проверить. Здесь — поведение самого
источника: порция, шардирование и то, что пустая выдача объясняется, а не
молчит.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.sources import Shard  # noqa: E402
from collector.sources.salts import SaltSource  # noqa: E402


class FakeStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.asked = []

    def wanted(self, limit):
        self.asked.append(limit)
        return self.rows[:limit]


def _rows(n, start=1000):
    return [(start + i, f"http://replay1.valve.net/570/{start + i}_7.dem.bz2")
            for i in range(n)]


def test_matches_with_a_salt_are_handed_to_the_pipeline():
    """Матч уходит в конвейер с ГОТОВЫМ адресом.

    Ни OpenDota, ни квота здесь не участвуют — в этом весь смысл.
    """
    src = SaltSource(FakeStore(_rows(1)), limit_per_cycle=5)
    refs = list(src.fetch_new())
    assert [r.match_id for r in refs] == [1000]
    assert refs[0].replay_url.endswith("1000_7.dem.bz2")


def test_the_portion_is_limited():
    """Порция ограничена: реплей весит 58 МиБ, и канал — узкое место.

    Двести реплеев — это 11 ГБ; выбрать их одним циклом значит занять
    канал на часы и обрушить всё остальное, что по нему ходит.
    """
    src = SaltSource(FakeStore(_rows(20)), limit_per_cycle=3)
    assert len(list(src.fetch_new())) == 3


def test_the_store_is_asked_with_a_reserve():
    """У базы берётся с запасом — из-за шардирования.

    На двухмашинной конфигурации половину выборки отсеет Shard, и без
    запаса цикл отдавал бы половину порции при полной очереди.
    """
    store = FakeStore(_rows(20))
    list(SaltSource(store, limit_per_cycle=3).fetch_new())
    assert store.asked == [12]


def test_foreign_shard_matches_are_skipped_without_downloading():
    """Чужой шард не качается.

    Обе машины качали бы одно и то же — 58 МиБ дважды за матч, при том
    что шардирование заведено ровно ради обратного.
    """
    store = FakeStore(_rows(8))
    src = SaltSource(store, limit_per_cycle=8,
                     shard=Shard(shard_id=0, count=2))
    got = [r.match_id for r in src.fetch_new()]
    assert got and all(m % 2 == 0 for m in got), got


def test_an_empty_queue_says_why(caplog):
    """Пустая выдача ОБЪЯСНЯЕТСЯ, а не молчит.

    «Качать нечего» и «добыча встала» выглядят одинаково — обе строчки
    пусты. Молчание, неотличимое от успеха, в этом проекте уже стоило
    тринадцати дней без бэкапов и целого спринта про пустую очередь
    кандидатов.
    """
    src = SaltSource(FakeStore([]), limit_per_cycle=5)
    with caplog.at_level("INFO"):
        assert list(src.fetch_new()) == []
    assert "gc-salts" in caplog.text, "не сказано, чем наполняется очередь"


def test_a_full_queue_stays_silent(caplog):
    """А когда работа есть — подсказка не печатается.

    Подсказка в каждом рабочем цикле превратилась бы в шум, а шум учит
    не читать лог.
    """
    src = SaltSource(FakeStore(_rows(3)), limit_per_cycle=5)
    with caplog.at_level("INFO"):
        list(src.fetch_new())
    assert "качать нечего" not in caplog.text


def test_the_source_is_registered_under_its_name():
    """Источник доступен как COLLECTOR_SOURCE=salts.

    Написанный, но не подключённый источник — это код, который никогда
    не выполнится, и заметить это можно только руками.
    """
    main = (pathlib.Path(__file__).resolve().parents[1]
            / "src" / "collector" / "__main__.py").read_text(encoding="utf-8")
    assert 'if name == "salts":' in main
    assert "SaltSource(" in main
    assert SaltSource.name == "salts"
