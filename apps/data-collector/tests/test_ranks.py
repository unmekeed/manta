"""Тесты кэша рангов и потока Valve (спринт 123).

Проверяется то, где ошибка была бы тихой и дорогой:
  * анонимный account_id Valve (0xFFFFFFFF) не должен попасть в кэш —
    иначе он навсегда занимает первое место в очереди опроса;
  * «ответ получен, ранга нет» и «спросить не удалось» — разные исходы,
    и слить их значит навсегда пометить живого игрока безранговым;
  * пакетный запрос STRATZ обязан УМЕТЬ оказаться админским и перейти на
    одиночные, а не падать (урок спринтов 121/122);
  * поиск хвоста последовательности обязан находить именно хвост —
    вызов без номера читает историю с 2011 года и выглядит успешным.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.ranks import (IMMORTAL_MIN_RANK, RANK_UNKNOWN,  # noqa: E402
                             OpenDotaRankResolver, StratzRankError,
                             StratzRankResolver, _rank_value, classify_match,
                             fill, is_admin_only, seed)
from collector.sources.steam import (ANONYMOUS_ACCOUNT_ID,  # noqa: E402
                                     SEQ_TIP_PRECISION, SteamAPIError,
                                     SteamMatchStream, match_accounts)


# -- поток Valve --------------------------------------------------------------

def _match(seq, accounts):
    return {"match_seq_num": seq,
            "players": [{"account_id": a} for a in accounts]}


def test_match_accounts_drops_anonymous_placeholder():
    m = _match(1, [111, ANONYMOUS_ACCOUNT_ID, 222])
    assert match_accounts(m) == [111, 222]


def test_match_accounts_drops_duplicates_and_nonpositive():
    m = _match(1, [111, 111, 0, -5, 222])
    assert match_accounts(m) == [111, 222]


def test_match_accounts_survives_garbage_ids():
    m = {"players": [{"account_id": "777"}, {"account_id": None},
                     {"account_id": "не число"}, {}]}
    assert match_accounts(m) == [777]


class FakeStream(SteamMatchStream):
    """Поток с известным хвостом; сеть не трогается."""

    def __init__(self, tip, batches=None):
        super().__init__("ключ", api_delay_s=0.0)
        self.tip = tip
        self.batches = batches or {}
        self.calls = []

    def batch(self, seq, count=100):
        self.calls.append((seq, count))
        if seq in self.batches:
            return self.batches[seq]
        return [{"match_seq_num": seq}] if seq <= self.tip else []


def test_tip_seq_finds_tail_within_precision():
    stream = FakeStream(tip=7_123_456_789)
    found = stream.tip_seq()
    assert 0 <= stream.tip - found <= SEQ_TIP_PRECISION


def test_tip_seq_never_reads_from_zero():
    """Вызов без номера читает 2011 год и выглядит успешным.

    Поиск обязан задавать номер В КАЖДОМ вызове; нулевой стартовый номер
    означал бы, что мы измеряем состав потока пятнадцатилетней давности.
    """
    stream = FakeStream(tip=1_000_000)
    stream.tip_seq()
    assert stream.calls, "поиск не сделал ни одного вызова"
    assert all(seq >= 1 for seq, _ in stream.calls)


def test_get_rejects_non_success_status(monkeypatch):
    import collector.sources.steam as steam_mod

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"status": 8, "statusDetail": "за пределом"}}

    monkeypatch.setattr(steam_mod.requests, "get", lambda *a, **k: Resp())
    stream = SteamMatchStream("ключ", api_delay_s=0.0)
    with pytest.raises(SteamAPIError, match="за пределом"):
        stream.batch(123)


# -- разбор ранга -------------------------------------------------------------

def test_rank_value_treats_missing_and_junk_as_unknown():
    assert _rank_value(None) == RANK_UNKNOWN
    assert _rank_value("мусор") == RANK_UNKNOWN
    assert _rank_value(-3) == RANK_UNKNOWN
    assert _rank_value(True) == RANK_UNKNOWN
    assert _rank_value("80") == 80


def test_is_admin_only_recognises_stratz_refusal():
    assert is_admin_only("User is not an admin.")
    assert not is_admin_only("Rate limit exceeded")


# -- решение по матчу ---------------------------------------------------------

def test_classify_requires_minimum_known_ranks():
    ranks = {1: 85, 2: 85, 3: None, 4: None, 5: 0}
    take, why = classify_match(ranks, min_known=4)
    assert not take and why == "мало известных рангов"


def test_classify_takes_match_when_known_are_immortal():
    ranks = {1: 85, 2: 82, 3: 80, 4: 81, 5: None}
    take, why = classify_match(ranks, min_known=4, min_share=0.6)
    assert take and why == "берём"


def test_classify_rejects_by_share_not_by_count():
    """Пять Immortal из десяти известных — это половина, а не большинство."""
    ranks = {i: (85 if i < 5 else 55) for i in range(10)}
    take, why = classify_match(ranks, min_known=4, min_share=0.6)
    assert not take and why == "низкий ранг"


def test_classify_ignores_unknown_ranks_in_share():
    """Ноль означает «спросили, ранга нет» — он не голосует ни за, ни против."""
    ranks = {1: 85, 2: 85, 3: 85, 4: 85, 5: 0, 6: 0, 7: 0, 8: 0}
    take, _ = classify_match(ranks, min_known=4, min_share=0.6)
    assert take


# -- резолвер OpenDota --------------------------------------------------------

class FakeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, **kwargs):
        aid = int(url.rsplit("/", 1)[1])
        self.asked.append(aid)
        return self.answers[aid]


def test_opendota_records_closed_profile_as_answered():
    """Профиль без ранга — это ОТВЕТ. Переспрашивать его завтра нечего."""
    r = OpenDotaRankResolver(api_delay_s=0.0)
    r._session = FakeSession({7: FakeResp(200, {"rank_tier": None})})
    assert r.resolve([7]) == {7: RANK_UNKNOWN}


def test_opendota_omits_failed_request_so_it_is_retried():
    """Сбой запроса не должен превратиться в «ранга нет» навсегда."""
    r = OpenDotaRankResolver(api_delay_s=0.0)
    r._session = FakeSession({7: FakeResp(500)})
    assert r.resolve([7]) == {}


def test_opendota_reads_rank_tier():
    r = OpenDotaRankResolver(api_delay_s=0.0)
    r._session = FakeSession({7: FakeResp(200, {"rank_tier": 84})})
    assert r.resolve([7]) == {7: 84}


# -- резолвер STRATZ ----------------------------------------------------------

class FakeStratz(StratzRankResolver):
    def __init__(self, replies, rank_field="seasonRank"):
        super().__init__("токен", batch_size=10, rank_field=rank_field)
        self.replies = replies
        self.queries = []

    def _gql(self, query, variables):
        kind = "batch" if "players(" in query else "single"
        self.queries.append((kind, variables))
        reply = self.replies[kind]
        if isinstance(reply, Exception):
            raise reply
        return reply(variables) if callable(reply) else reply


def _account(aid, rank):
    return {"steamAccount": {"id": aid, "seasonRank": rank}}


def test_stratz_uses_batch_when_allowed():
    r = FakeStratz({"batch": {"players": [_account(1, 85), _account(2, 40)]}})
    assert r.resolve([1, 2]) == {1: 85, 2: 40}
    assert r.batch_allowed is True
    assert [k for k, _ in r.queries] == ["batch"]


def test_stratz_marks_unlisted_account_as_answered():
    """Игрок, которого STRATZ не знает, в ответе не появляется вовсе."""
    r = FakeStratz({"batch": {"players": [_account(1, 85)]}})
    assert r.resolve([1, 2]) == {1: 85, 2: RANK_UNKNOWN}


def test_stratz_falls_back_to_singles_when_batch_is_admin_only():
    r = FakeStratz({
        "batch": StratzRankError("User is not an admin."),
        "single": lambda v: {"player": _account(v["id"], 81)},
    })
    assert r.resolve([1, 2]) == {1: 81, 2: 81}
    assert r.batch_allowed is False
    assert [k for k, _ in r.queries] == ["batch", "single", "single"]


def test_stratz_does_not_retry_batch_after_refusal():
    """Отказ админского эндпоинта — приговор, а не временный сбой."""
    r = FakeStratz({
        "batch": StratzRankError("User is not an admin."),
        "single": lambda v: {"player": _account(v["id"], 81)},
    })
    r.resolve([1])
    r.resolve([2])
    assert [k for k, _ in r.queries].count("batch") == 1


def test_stratz_single_failure_omits_account():
    r = FakeStratz({
        "batch": StratzRankError("User is not an admin."),
        "single": StratzRankError("Rate limit exceeded"),
    })
    assert r.resolve([1, 2]) == {}


# -- fill / seed --------------------------------------------------------------

class FakeCache:
    def __init__(self, pending=None, stale=None):
        self._pending = list(pending or [])
        self._stale = list(stale or [])
        self.saved = []
        self.seen = []
        self.cursor = None

    def pending(self, limit):
        return self._pending[:max(limit, 0)]

    def stale(self, limit, ttl_days=30):
        return self._stale[:max(limit, 0)]

    def save(self, resolved, source):
        self.saved.append((dict(resolved), source))
        return len(resolved)

    def see(self, ids):
        ids = list(ids)
        self.seen.extend(ids)
        return len({i for i in ids if i > 0 and i != ANONYMOUS_ACCOUNT_ID})

    def get_cursor(self):
        return self.cursor

    def set_cursor(self, seq):
        self.cursor = seq


class FakeResolver:
    name = "fake"

    def __init__(self, ranks=None, silent=False):
        self.ranks = ranks or {}
        self.silent = silent
        self.asked = []

    def resolve(self, ids):
        self.asked.extend(ids)
        if self.silent:
            return {}
        return {i: self.ranks.get(i, RANK_UNKNOWN) for i in ids}


def test_fill_never_exceeds_budget():
    cache = FakeCache(pending=list(range(100)), stale=list(range(200, 300)))
    resolver = FakeResolver()
    stats = fill(cache, resolver, budget=10)
    assert stats["спрошено"] == 10
    assert len(resolver.asked) == 10


def test_fill_reserves_part_of_budget_for_refresh():
    """Без резерва обновление не случилось бы никогда: новых всегда больше."""
    cache = FakeCache(pending=list(range(100)), stale=[900, 901, 902])
    fill(cache, FakeResolver(), budget=10, stale_share=0.2)
    asked = cache.saved[0][0]
    assert 900 in asked and 901 in asked


def test_fill_spends_full_budget_when_one_queue_is_short():
    cache = FakeCache(pending=[1, 2], stale=list(range(900, 950)))
    stats = fill(cache, FakeResolver(), budget=10, stale_share=0.2)
    assert stats["спрошено"] == 10


def test_fill_counts_immortals_only():
    cache = FakeCache(pending=[1, 2, 3])
    resolver = FakeResolver({1: IMMORTAL_MIN_RANK, 2: 79, 3: 85})
    stats = fill(cache, resolver, budget=3)
    assert stats["immortal"] == 2


def test_fill_does_not_save_when_nothing_resolved():
    """Пустой ответ не должен превратиться в запись «ранга нет»."""
    cache = FakeCache(pending=[1, 2, 3])
    stats = fill(cache, FakeResolver(silent=True), budget=3)
    assert cache.saved == []
    assert stats["получено"] == 0


def test_seed_advances_cursor_past_last_match():
    """Курсор берётся из ДАННЫХ, а не из счётчика прочитанных матчей.

    Номера намеренно не подряд: если двигать курсор на «сколько матчей
    прочитали», на любой дырке в последовательности сборщик встанет и
    начнёт перечитывать один и тот же кусок вечно.
    """
    cache = FakeCache()
    cache.cursor = 500
    stream = FakeStream(tip=10**9, batches={
        500: [_match(500, [11, 12]), _match(507, [13])],
        508: [],
    })
    stats = seed(cache, stream, matches=100)
    assert cache.cursor == 508
    assert stats["матчей"] == 2


def test_seed_stops_at_live_edge_without_looping():
    cache = FakeCache()
    cache.cursor = 700
    stream = FakeStream(tip=10**9, batches={700: []})
    stats = seed(cache, stream, matches=1000)
    assert stats["матчей"] == 0
    assert stats["вызовов"] == 1


def test_seed_finds_tip_when_cursor_is_empty():
    """Пустой курсор обязан привести к поиску хвоста, а не к чтению с нуля."""
    cache = FakeCache()
    stream = FakeStream(tip=4_000_000)
    seed(cache, stream, matches=1)
    assert cache.cursor is not None and cache.cursor > 3_000_000


def test_seed_keeps_repeat_encounters_for_priority():
    """seen_count — это приоритет опроса, дубли между матчами не схлопываются."""
    cache = FakeCache()
    cache.cursor = 10
    stream = FakeStream(tip=10**9, batches={
        10: [_match(10, [42, 7]), _match(11, [42, 8])],
        12: [],
    })
    seed(cache, stream, matches=100)
    assert cache.seen.count(42) == 2
