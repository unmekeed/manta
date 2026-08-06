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
                             _BatchShrunk,
                             OpenDotaRankResolver, StratzRankError,
                             StratzRankResolver, _rank_value, build_resolver,
                             classify_match, fill, harvest_rawstore,
                             is_admin_only, rawstore_pairs, seed,
                             take_limit,
                             visible_per_match)
from collector.sources.steam import (ANONYMOUS_ACCOUNT_ID,  # noqa: E402
                                     SEQ_TIP_PRECISION, SteamAPIError,
                                     SteamMatchStream, SteamRateLimited,
                                     match_account_stats, match_accounts)


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


def test_match_account_stats_counts_hidden_profiles():
    """Доля скрытых профилей — параметр проекта, а не любопытство."""
    m = _match(1, [11, ANONYMOUS_ACCOUNT_ID, ANONYMOUS_ACCOUNT_ID, 22])
    accounts, slots, hidden = match_account_stats(m)
    assert accounts == [11, 22]
    assert slots == 4
    assert hidden == 2


def test_visible_per_match_uses_slots_not_distinct_accounts():
    stats = {"матчей": 100, "слотов": 1000, "скрытых": 700}
    assert visible_per_match(stats) == 3.0


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body if body is not None else {"result": {"status": 1,
                                                               "matches": []}}

    def json(self):
        return self._body


def _steam_with_codes(monkeypatch, codes):
    """Поток, чьи ответы заданы списком HTTP-кодов."""
    import collector.sources.steam as steam_mod
    seq = list(codes)
    calls = []

    def fake_get(url, **kwargs):
        code = seq.pop(0) if seq else 200
        calls.append(code)
        return _Resp(code)

    monkeypatch.setattr(steam_mod.requests, "get", fake_get)
    monkeypatch.setattr(steam_mod.time, "sleep", lambda s: None)
    return SteamMatchStream("ключ", api_delay_s=0.0, backoff_base_s=0.0), calls


def test_rate_limit_is_retried_not_surrendered(monkeypatch):
    """429 после двенадцати вызовов — замер 2026-08-06; пауза не спасает."""
    stream, calls = _steam_with_codes(monkeypatch, [429, 429, 200])
    assert stream.batch(100) == []
    assert calls == [429, 429, 200]
    assert stream.rate_limit_waits == 2


def test_rate_limit_gives_up_after_all_backoffs(monkeypatch):
    stream, calls = _steam_with_codes(monkeypatch, [429] * 10)
    stream._retries = 3
    with pytest.raises(SteamRateLimited):
        stream.batch(100)
    assert len(calls) == 4, "три отступления = четыре попытки"


def test_non_429_error_is_not_retried(monkeypatch):
    """Отступать на 500 бессмысленно — это не лимит, а поломка."""
    stream, calls = _steam_with_codes(monkeypatch, [500, 200])
    with pytest.raises(SteamAPIError):
        stream.batch(100)
    assert calls == [500]


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


def test_take_limit_read_from_stratz_message():
    assert take_limit("You have surpassed the maximum take value of : 5") == 5
    assert take_limit("Rate limit exceeded") is None


def test_stratz_shrinks_batch_and_retries_same_chunk():
    """Пакет разрешён, но не больше пяти — предел приходит из ответа."""
    calls = []

    class Limited(FakeStratz):
        def _gql(self, query, variables):
            ids = variables.get("ids", [])
            calls.append(len(ids))
            if len(ids) > 5:
                raise StratzRankError(
                    "You have surpassed the maximum take value of : 5")
            return {"players": [_account(i, 85) for i in ids]}

    r = Limited({}, )
    r._batch_size = 10
    got = r.resolve(list(range(1, 11)))
    assert set(got) == set(range(1, 11)), "ужатая пачка обязана добрать всех"
    assert calls == [10, 5, 5], calls


def test_stratz_gives_up_when_limit_does_not_shrink_batch():
    """Сервер, повторяющий один и тот же предел, не должен вешать процесс."""
    class Stuck(FakeStratz):
        def _gql(self, query, variables):
            self.queries.append(("batch", variables))
            raise StratzRankError(
                "You have surpassed the maximum take value of : 5")

    r = Stuck({})
    r._batch_size = 10
    r._try_batch = lambda ids: (_ for _ in ()).throw(_BatchShrunk())
    assert r.resolve(list(range(20))) == {}
    assert any("не уменьшилась" in k or "не уменьшился" in k
               for k in r.failures), r.failures


def test_stratz_does_not_flood_singles_on_transient_batch_error():
    """Сбой пачки из-за квоты не должен превращаться в 50 одиночных."""
    r = FakeStratz({"batch": StratzRankError("HTTP 429 после отступлений"),
                    "single": lambda v: {"player": _account(v["id"], 85)}})
    r._batch_size = 50
    assert r.resolve(list(range(50))) == {}
    assert [k for k, _ in r.queries] == ["batch"], r.queries


def test_stratz_backs_off_on_429():
    class Resp:
        def __init__(self, code, body):
            self.status_code = code
            self.content = b"x"
            self._body = body

        def json(self):
            return self._body

    seq = [Resp(429, {}), Resp(429, {}),
           Resp(200, {"data": {"players": [_account(1, 85)]}})]
    posted = []

    class Session:
        def post(self, url, **kwargs):
            posted.append(kwargs)
            return seq.pop(0)

    import collector.ranks as ranks_mod
    r = StratzRankResolver("токен", api_delay_s=0.0, backoff_base_s=0.0)
    r._session = Session()
    saved, ranks_mod.time.sleep = ranks_mod.time.sleep, lambda s: None
    try:
        assert r.resolve([1]) == {1: 85}
    finally:
        ranks_mod.time.sleep = saved
    assert len(posted) == 3, "два отступления и успех"


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


def test_fill_reports_why_accounts_stayed_silent():
    """«спрошено 200, получено 76» без причины — не диагностика."""
    class Failing(FakeResolver):
        def resolve(self, ids):
            self.failures = __import__("collections").Counter({"HTTP 429": len(ids)})
            return {}

    cache = FakeCache(pending=[1, 2, 3])
    stats = fill(cache, Failing(silent=True), budget=3)
    assert any("HTTP 429" in k for k in stats), stats


def test_build_resolver_prefers_stratz_when_token_present(monkeypatch):
    """Пакет STRATZ закрывает 50 игроков за запрос, OpenDota — одного."""
    monkeypatch.delenv("RANKS_RESOLVER", raising=False)
    monkeypatch.setenv("STRATZ_API_TOKEN", "токен")
    assert build_resolver().name == "stratz"


def test_build_resolver_falls_back_without_token(monkeypatch):
    monkeypatch.delenv("RANKS_RESOLVER", raising=False)
    monkeypatch.delenv("STRATZ_API_TOKEN", raising=False)
    assert build_resolver().name == "opendota"


def test_build_resolver_obeys_explicit_choice(monkeypatch):
    monkeypatch.setenv("STRATZ_API_TOKEN", "токен")
    assert build_resolver("opendota").name == "opendota"


def test_seed_counts_hidden_profiles_across_stream():
    cache = FakeCache()
    cache.cursor = 300
    stream = FakeStream(tip=10**9, batches={
        300: [_match(300, [1, ANONYMOUS_ACCOUNT_ID, ANONYMOUS_ACCOUNT_ID])],
        301: [],
    })
    stats = seed(cache, stream, matches=10)
    assert stats["слотов"] == 3 and stats["скрытых"] == 2
    assert visible_per_match(stats) == 1.0


# -- посев из сохранённого JSON ------------------------------------------------

def _json_match(start_time, players):
    return {"start_time": start_time,
            "players": [{"account_id": a, "rank_tier": r} for a, r in players]}


def test_rawstore_pairs_reads_per_player_rank():
    """В сыром JSON ранг лежит у КАЖДОГО игрока, а не средним по матчу."""
    pairs = rawstore_pairs(_json_match(1_700_000_000, [(11, 80), (22, 54)]))
    assert [(a, r) for a, r, _ in pairs] == [(11, 80), (22, 54)]
    assert all(w.year >= 2023 for _, _, w in pairs)


def test_rawstore_pairs_skips_match_without_date():
    """Ранг без даты пришлось бы выдать за сегодняшний — это ложь."""
    assert rawstore_pairs({"players": [{"account_id": 11, "rank_tier": 80}]}) == []


def test_rawstore_pairs_skips_players_without_rank():
    pairs = rawstore_pairs(_json_match(1_700_000_000,
                                       [(11, None), (22, 0), (33, 81)]))
    assert [a for a, _, _ in pairs] == [33]


def test_rawstore_pairs_skips_anonymous():
    pairs = rawstore_pairs(_json_match(1_700_000_000,
                                       [(ANONYMOUS_ACCOUNT_ID, 80), (33, 81)]))
    assert [a for a, _, _ in pairs] == [33]


class FakeStore:
    def __init__(self, matches):
        self._m = matches

    def iter_match_ids(self):
        return iter(self._m)

    def get(self, mid):
        return self._m[mid]


class DatedCache(FakeCache):
    def __init__(self):
        super().__init__()
        self.dated = []

    def save(self, resolved, source, dates=None):
        self.dated.append((dict(resolved), dict(dates or {})))
        return len(resolved)


def test_harvest_keeps_freshest_rank_per_account():
    """Порядок обхода бакета не должен решать, какой ранг победит."""
    store = FakeStore({
        1: _json_match(1_600_000_000, [(7, 70)]),   # старый матч
        2: _json_match(1_700_000_000, [(7, 82)]),   # свежий
        3: _json_match(1_650_000_000, [(7, 75)]),   # промежуточный, после свежего
    })
    cache = DatedCache()
    stats = harvest_rawstore(cache, store)
    saved, dates = cache.dated[0]
    assert saved == {7: 82}, saved
    assert dates[7].timestamp() == 1_700_000_000
    assert stats["пар"] == 3 and stats["аккаунтов"] == 1


def test_harvest_counts_immortals():
    store = FakeStore({1: _json_match(1_700_000_000,
                                      [(1, 80), (2, 85), (3, 55)])})
    stats = harvest_rawstore(DatedCache(), store)
    assert stats["immortal"] == 2 and stats["аккаунтов"] == 3


def test_harvest_respects_limit():
    store = FakeStore({i: _json_match(1_700_000_000, [(i, 80)])
                       for i in range(10)})
    stats = harvest_rawstore(DatedCache(), store, limit=3)
    assert stats["матчей"] == 3


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
