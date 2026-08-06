"""Тесты очереди кандидатов и источника своей разбивки (спринт 126).

Проверяется то, где ошибка была бы тихой и дорогой:
  * отсутствие соли у свежего матча — НЕ ошибка, а повод отложить; без
    этого коллектор либо жёг бы квоту на одном матче каждый цикл, либо
    выбрасывал бы годные матчи;
  * повторная находка не должна возвращать в очередь уже скачанный матч,
    иначе перезапуск сканера означает повторную закачку 58 МиБ;
  * состояние «скачан» ставится ПОСЛЕ скачивания, а не при выдаче.
"""
import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.candidates import Candidate  # noqa: E402
from collector.sources import PermanentDownloadError, Shard  # noqa: E402
from collector.sources.candidates import CandidateSource  # noqa: E402


def _cand(mid, seq=1000):
    return Candidate(match_id=mid, match_seq_num=seq, started_at=None,
                     known_ranks=3, immortal_ranks=3, avg_known_rank=82)


class FakeQueue:
    """Очередь в памяти с той же семантикой состояний, что в SQL."""

    def __init__(self, ready=()):
        self.rows = {c.match_id: {"state": "new", "attempts": 0,
                                  "cand": c, "error": None}
                     for c in ready}
        self.expired = 0
        self.stale_taken = 0

    def requeue_stale_taken(self, minutes=30):
        return self.stale_taken

    def expire(self, ttl_days=13):
        return self.expired

    def take(self, limit):
        return [r["cand"] for r in self.rows.values()
                if r["state"] == "new"][:limit]

    def mark(self, match_id, state, error=None):
        self.rows[match_id]["state"] = state
        self.rows[match_id]["error"] = error

    def defer(self, match_id, minutes=30, error=None, max_attempts=8):
        row = self.rows[match_id]
        row["attempts"] += 1
        row["error"] = error
        row["state"] = "no_salt" if row["attempts"] >= max_attempts else "new"
        return row["state"]


class FakeOpenDota:
    def __init__(self, details, payload=b"dem"):
        self.details = details
        self.asked = []
        self.downloaded = []
        self.payload = payload

    def _match_detail(self, match_id):
        self.asked.append(match_id)
        value = self.details.get(match_id)
        if isinstance(value, Exception):
            raise value
        return value

    def download_replay(self, ref):
        self.downloaded.append(ref.match_id)
        return self.payload


def _source(queue, details, limit=10, **kw):
    src = CandidateSource(queue, limit_per_cycle=limit, **kw)
    src._od = FakeOpenDota(details)
    return src


# -- выдача ---------------------------------------------------------------------

def test_yields_match_with_ready_salt():
    q = FakeQueue([_cand(1)])
    src = _source(q, {1: {"replay_url": "http://r/1.dem.bz2", "patch": 57}})
    refs = list(src.fetch_new())
    assert [r.match_id for r in refs] == [1]
    assert refs[0].replay_url == "http://r/1.dem.bz2"
    assert refs[0].patch == 57
    assert q.rows[1]["state"] == "taken"


def test_match_without_salt_is_deferred_not_dropped():
    """Соль появляется с задержкой — матч обязан вернуться в очередь."""
    q = FakeQueue([_cand(1)])
    src = _source(q, {1: {"replay_url": None}})
    assert list(src.fetch_new()) == []
    assert q.rows[1]["state"] == "new", "матч потерян"
    assert q.rows[1]["attempts"] == 1
    assert src.last_cycle["нет соли"] == 1


def test_match_gives_up_after_attempt_budget():
    q = FakeQueue([_cand(1)])
    q.rows[1]["attempts"] = 7
    src = _source(q, {1: {}})
    list(src.fetch_new())
    assert q.rows[1]["state"] == "no_salt"
    assert src.last_cycle["безнадёжных"] == 1


def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=resp)


def test_quota_exhaustion_aborts_the_cycle_immediately():
    """429 — состояние ЦИКЛА, а не свойство матча.

    Живой прогон 2026-08-06: цикл шёл к следующему кандидату и сжигал
    шестьдесят запросов на исчерпанной квоте («взято 60, отдано 0, нет
    соли 60»), дожигая и без того отрицательный остаток.
    """
    q = FakeQueue([_cand(i) for i in range(10)])
    src = _source(q, {i: _http_error(429) for i in range(10)})
    with pytest.raises(requests.HTTPError):
        list(src.fetch_new())
    assert len(src._od.asked) == 1, "цикл продолжил жечь квоту"
    assert src.last_cycle["квота исчерпана"] == 1


def test_quota_exhaustion_does_not_count_as_a_failed_attempt():
    """Матч ни в чём не виноват — простой не должен его забраковывать.

    Иначе через восемь циклов простоя очередь целиком уходит в no_salt.
    """
    q = FakeQueue([_cand(1)])
    src = _source(q, {1: _http_error(429)})
    with pytest.raises(requests.HTTPError):
        list(src.fetch_new())
    assert q.rows[1]["attempts"] == 0
    assert q.rows[1]["state"] == "new"


def test_non_429_http_error_still_defers_that_match():
    """500 у одного матча — его личная беда, цикл продолжается."""
    q = FakeQueue([_cand(1), _cand(2)])
    src = _source(q, {1: _http_error(500),
                      2: {"replay_url": "http://r/2", "players": []}})
    refs = list(src.fetch_new())
    assert [r.match_id for r in refs] == [2]
    assert q.rows[1]["attempts"] == 1


def test_network_error_defers_and_continues_with_next():
    """Сбой сети на одном матче не должен обрывать весь цикл."""
    q = FakeQueue([_cand(1), _cand(2)])
    src = _source(q, {1: requests.ConnectionError("обрыв"),
                      2: {"replay_url": "http://r/2.dem.bz2"}})
    refs = list(src.fetch_new())
    assert [r.match_id for r in refs] == [2]
    assert q.rows[1]["state"] == "new" and q.rows[1]["attempts"] == 1


def test_cycle_stops_at_limit():
    q = FakeQueue([_cand(i) for i in range(10)])
    src = _source(q, {i: {"replay_url": f"http://r/{i}"} for i in range(10)},
                  limit=3)
    assert len(list(src.fetch_new())) == 3


def test_takes_with_reserve_so_deferrals_do_not_empty_the_cycle():
    """Половина кандидатов без соли не должна оставлять цикл пустым."""
    q = FakeQueue([_cand(i) for i in range(6)])
    details = {i: ({"replay_url": f"http://r/{i}"} if i % 2 else {"replay_url": None})
               for i in range(6)}
    src = _source(q, details, limit=3)
    refs = list(src.fetch_new())
    assert len(refs) == 3, "запас не сработал, цикл недобрал"


def test_shard_skips_foreign_matches_without_spending_quota():
    q = FakeQueue([_cand(1), _cand(2)])
    src = _source(q, {1: {"replay_url": "http://r/1"},
                      2: {"replay_url": "http://r/2"}},
                  shard=Shard(shard_id=0, count=2))
    refs = list(src.fetch_new())
    assert [r.match_id for r in refs] == [2]
    assert src._od.asked == [2], "у чужого шарда спрашивали детали"


# -- скачивание -----------------------------------------------------------------

def test_done_is_set_only_after_download():
    """Между выдачей и скачиванием лежат 58 МиБ; обрыв — не успех."""
    q = FakeQueue([_cand(1)])
    src = _source(q, {1: {"replay_url": "http://r/1"}})
    ref = next(iter(src.fetch_new()))
    assert q.rows[1]["state"] == "taken", "скачан ещё до скачивания"
    src.download_replay(ref)
    assert q.rows[1]["state"] == "done"


def test_download_failure_returns_candidate_to_the_queue():
    """Сбой скачивания не должен терять кандидата навсегда.

    Живой прогон 2026-08-06: кандидат помечался taken при выдаче, а при
    418 от реплей-сервера Valve там и оставался — очередь выбирает только
    new, поэтому матч не повторялся никогда. За полтора часа так зависло
    шесть штук.
    """
    q = FakeQueue([_cand(1)])
    src = _source(q, {1: {"replay_url": "http://r/1"}})
    ref = next(iter(src.fetch_new()))

    def boom(_ref):
        raise requests.ConnectionError("обрыв на 40 МиБ")

    src._od.download_replay = boom
    with pytest.raises(requests.ConnectionError):
        src.download_replay(ref)
    assert q.rows[1]["state"] == "new", "кандидат завис в taken"
    assert q.rows[1]["attempts"] == 1


def test_permanent_download_error_closes_candidate_forever():
    """404/410/битый архив — возвращать в очередь нечего."""
    q = FakeQueue([_cand(1)])
    src = _source(q, {1: {"replay_url": "http://r/1"}})
    ref = next(iter(src.fetch_new()))

    def gone(_ref):
        raise PermanentDownloadError("реплей удалён")

    src._od.download_replay = gone
    with pytest.raises(PermanentDownloadError):
        src.download_replay(ref)
    assert q.rows[1]["state"] == "failed"


# -- факт из ответа за соль -----------------------------------------------------

class TruthQueue(FakeQueue):
    def __init__(self, ready=()):
        super().__init__(ready)
        self.truth = {}

    def record_truth(self, match_id, known, immortal, avg_rank):
        self.truth[match_id] = (known, immortal, avg_rank)


class SpyCache:
    def __init__(self):
        self.saved = []

    def save(self, ranks, source, dates=None):
        self.saved.append((dict(ranks), source))
        return len(ranks)


def _detail(url, players):
    return {"replay_url": url,
            "players": [{"account_id": a, "rank_tier": r} for a, r in players]}


def test_records_true_composition_from_the_salt_response():
    """Факт приезжает вместе с солью и не стоит лишнего вызова."""
    q = TruthQueue([_cand(1)])
    src = _source(q, {1: _detail("http://r/1", [(11, 80), (12, 80), (13, 75)])})
    list(src.fetch_new())
    assert q.truth[1] == (3, 2, 78), q.truth
    assert src.last_cycle["факт записан"] == 1


def test_feeds_true_ranks_into_the_cache():
    """Каждая закачка отдаёт до десяти пар «аккаунт -> ранг» даром."""
    q = TruthQueue([_cand(1)])
    cache = SpyCache()
    src = _source(q, {1: _detail("http://r/1", [(11, 80), (12, 55)])},
                  cache=cache)
    list(src.fetch_new())
    assert cache.saved == [({11: 80, 12: 55}, "opendota-match")]
    assert src.last_cycle["рангов в кэш"] == 2


def test_anonymous_players_do_not_reach_the_cache_but_count_in_truth():
    """Скрытый профиль — это игрок матча, но не адресуемый аккаунт."""
    from collector.sources.steam import ANONYMOUS_ACCOUNT_ID
    q = TruthQueue([_cand(1)])
    cache = SpyCache()
    src = _source(q, {1: _detail("http://r/1",
                                 [(ANONYMOUS_ACCOUNT_ID, 80), (12, 80)])},
                  cache=cache)
    list(src.fetch_new())
    assert q.truth[1][0] == 2, "аноним обязан считаться в составе матча"
    assert cache.saved == [({12: 80}, "opendota-match")]


def test_no_ranks_in_response_records_nothing():
    """Пустой факт не должен выглядеть как «матч из безранговых»."""
    q = TruthQueue([_cand(1)])
    src = _source(q, {1: _detail("http://r/1", [(11, None), (12, 0)])})
    list(src.fetch_new())
    assert q.truth == {}
    assert "факт записан" not in src.last_cycle


def test_source_works_without_cache():
    q = TruthQueue([_cand(1)])
    src = _source(q, {1: _detail("http://r/1", [(11, 80)])})
    assert len(list(src.fetch_new())) == 1
    assert "рангов в кэш" not in src.last_cycle


def test_stale_taken_candidates_are_requeued():
    """Процесс умер между выдачей и скачиванием — очередь не должна течь."""
    q = FakeQueue([_cand(1)])
    q.stale_taken = 3
    src = _source(q, {})
    list(src.fetch_new())
    assert src.last_cycle["зависших вернули"] == 3


def test_expired_are_counted_in_cycle_stats():
    q = FakeQueue([])
    q.expired = 4
    src = _source(q, {})
    list(src.fetch_new())
    assert src.last_cycle["просрочено"] == 4
