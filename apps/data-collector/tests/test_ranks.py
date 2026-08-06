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
                             STRATZ_HOUR_LIMIT, StratzQuotaExhausted,
                             SWEEP_KNOWN, SWEEP_SHARE, is_admin_only,
                             _verify_forward, lag_seconds,
                             queue_report, rawstore_pairs, scan, seed,
                             stream_filter,
                             stream_rate,
                             sweep_table,
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


class _StratzResp:
    def __init__(self, code, body, headers=None):
        self.status_code = code
        self.content = b"x"
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class _StratzSession:
    def __init__(self, seq):
        self.seq = list(seq)
        self.posted = []

    def post(self, url, **kwargs):
        self.posted.append(kwargs)
        return self.seq.pop(0) if self.seq else self.seq[-1]


def _quiet_sleep(monkeypatch):
    import collector.ranks as ranks_mod
    monkeypatch.setattr(ranks_mod.time, "sleep", lambda s: None)


def test_quota_exhaustion_is_not_retried(monkeypatch):
    """429 при нулевом остатке часа — не всплеск, отступать бессмысленно.

    Живой прогон 2026-08-06: после исчерпания часовой квоты цикл ушёл в
    часы попыток по 30 секунд каждая, потому что код не отличал
    исчерпанную квоту от всплеска.
    """
    _quiet_sleep(monkeypatch)
    r = StratzRankResolver("токен", api_delay_s=0.0, backoff_base_s=0.0)
    r._session = _StratzSession([
        _StratzResp(429, {}, {"x-ratelimit-remaining-hour": "0"})])
    with pytest.raises(StratzQuotaExhausted):
        r.resolve([1])
    assert len(r._session.posted) == 1, "на исчерпанной квоте были повторы"


def test_persistent_429_without_headers_is_treated_as_quota(monkeypatch):
    _quiet_sleep(monkeypatch)
    r = StratzRankResolver("токен", api_delay_s=0.0, backoff_base_s=0.0,
                           retries=2)
    r._session = _StratzSession([_StratzResp(429, {}) for _ in range(5)])
    with pytest.raises(StratzQuotaExhausted):
        r.resolve([1])


def test_run_budget_caps_requests(monkeypatch):
    """Квота часа общая с коллектором матчей — выесть её нельзя."""
    _quiet_sleep(monkeypatch)
    r = StratzRankResolver("токен", api_delay_s=0.0, batch_size=1,
                           run_budget=3)
    r._session = _StratzSession(
        [_StratzResp(200, {"data": {"players": [_account(i, 85)]}})
         for i in range(10)])
    with pytest.raises(StratzQuotaExhausted):
        r.resolve(list(range(10)))
    assert len(r._session.posted) == 3


def test_default_pace_respects_the_binding_hour_limit():
    """Считать паузу надо по 1500/час, а не по 8/с."""
    r = StratzRankResolver("токен")
    assert r._api_delay_s >= 3600.0 / STRATZ_HOUR_LIMIT


def test_fill_stops_on_quota_and_keeps_what_it_got():
    class Quota(FakeResolver):
        def resolve(self, ids):
            self.asked.extend(ids)
            if len(self.asked) > 2:
                raise StratzQuotaExhausted("квота исчерпана")
            return {i: 85 for i in ids}

    cache = FakeCache(pending=list(range(20)))
    stats = fill(cache, Quota(), budget=20, chunk=2)
    assert stats.get("остановлен") == 1
    assert stats["получено"] == 2, "потеряли уже полученные ранги"
    assert len(cache.saved) == 1
    # «спрошено» — сколько РЕАЛЬНО отправлено, а не сколько запланировано.
    # Иначе отчёт «спрошено 5000, получено 0» после остановки на первом
    # куске читается как пять тысяч потраченных впустую запросов.
    assert stats["в очереди"] == 20
    assert stats["спрошено"] == 4, stats


def test_stratz_backs_off_on_429():
    seq = [_StratzResp(429, {}), _StratzResp(429, {}),
           _StratzResp(200, {"data": {"players": [_account(1, 85)]}})]
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


# -- отбор матчей ---------------------------------------------------------------

def _stream_match(seq, accounts, lobby=7, mode=22, duration=2000):
    return {"match_seq_num": seq, "lobby_type": lobby, "game_mode": mode,
            "duration": duration,
            "players": [{"account_id": a} for a in accounts]}


def test_stream_filter_rejects_turbo_and_short():
    assert stream_filter(_stream_match(1, [1], lobby=0)) == (False, "не ранкед")
    assert stream_filter(_stream_match(1, [1], mode=23)) == (False, "не all pick")
    assert stream_filter(_stream_match(1, [1], duration=300))[1] == "короткий"
    assert stream_filter(_stream_match(1, [1]))[0]


def test_default_threshold_is_reachable_at_measured_visibility():
    """3.8 видимых игрока из 10 — порог обязан быть достижим."""
    ranks = {1: 85, 2: 84, 3: None}
    assert classify_match(ranks)[0], "умолчание требует больше рангов, чем видно"


class ScanCache(FakeCache):
    def __init__(self, ranks):
        super().__init__()
        self._ranks = ranks
        self.rank_queries = 0

    def ranks_of(self, ids):
        self.rank_queries += 1
        return {a: self._ranks[a] for a in ids if a in self._ranks}


def _scan_stream(batches):
    return FakeStream(tip=10**9, batches=batches)


def test_scan_counts_funnel_by_reason():
    cache = ScanCache({1: 85, 2: 84, 5: 40, 6: 41})
    cache.cursor = 10
    stream = _scan_stream({
        10: [_stream_match(10, [1, 2]),          # берём
             _stream_match(11, [5, 6]),          # низкий ранг
             _stream_match(12, [7, 8]),          # рангов не знаем
             _stream_match(13, [1, 2], lobby=0)],  # не ранкед
        14: [],
    })
    funnel, _ = scan(cache, stream, matches=100)
    assert funnel["берём"] == 1
    assert funnel["низкий ранг"] == 1
    assert funnel["мало известных рангов"] == 1
    assert funnel["не ранкед"] == 1
    assert funnel["всего матчей"] == 4


def test_scan_asks_ranks_once_per_batch_not_per_match():
    """Сотня round-trip'ов вместо одного IN — разница в разы на длинном прогоне."""
    cache = ScanCache({})
    cache.cursor = 10
    stream = _scan_stream({
        10: [_stream_match(10 + i, [i]) for i in range(20)],
        30: [],
    })
    scan(cache, stream, matches=100)
    assert cache.rank_queries == 1, cache.rank_queries


def test_scan_feeds_cache_even_with_rejected_matches():
    """Проход по потоку сделан — аккаунты забирать надо все, а не только годных."""
    cache = ScanCache({})
    cache.cursor = 10
    stream = _scan_stream({
        10: [_stream_match(10, [111, 222], lobby=0)],   # отбракован режимом
        11: [],
    })
    scan(cache, stream, matches=100)
    assert set(cache.seen) == {111, 222}


def test_sweep_shows_stricter_thresholds_pass_fewer():
    cache = ScanCache({1: 85, 2: 84, 3: 40})
    cache.cursor = 10
    stream = _scan_stream({10: [_stream_match(10, [1, 2, 3])], 11: []})
    _, sweep = scan(cache, stream, matches=100)
    # 2 из 3 известных — immortal: доля 0.67
    assert sweep[(2, 0.5)] == 1 and sweep[(2, 0.75)] == 0
    assert sweep[(4, 0.5)] == 0, "четырёх известных тут нет"


def test_sweep_table_renders_percentages():
    sweep = {(k, s): 0 for k in SWEEP_KNOWN for s in SWEEP_SHARE}
    sweep[(2, 0.5)] = 25
    out = sweep_table(sweep, 100)
    assert "25.00%" in out and "50%" in out


# -- догон живого края ------------------------------------------------------------

def _timed(seq, mid, start_time, accounts=(1,)):
    return {"match_seq_num": seq, "match_id": mid, "lobby_type": 7,
            "game_mode": 22, "duration": 2400, "start_time": start_time,
            "players": [{"account_id": a} for a in accounts]}


def test_stream_rate_measured_from_the_batch_itself():
    """Темп потока берётся из уже прочитанных данных, без лишних вызовов."""
    got = [_timed(1000 + i * 10, i, 1_700_000_000 + i) for i in range(100)]
    rate = stream_rate(got)
    assert rate is not None and abs(rate - 10.0) < 1e-6


def test_stream_rate_is_none_when_time_does_not_advance():
    got = [_timed(1000 + i, i, 1_700_000_000) for i in range(5)]
    assert stream_rate(got) is None


def test_lag_seconds_uses_the_freshest_match():
    now = 1_700_010_000
    got = [_timed(1, 1, now - 7200), _timed(2, 2, now - 3600)]
    assert lag_seconds(got, now) == 3600


def test_verify_forward_falls_back_when_target_is_empty():
    """Перелёт за живой край оставил бы курсор в пустоте навсегда."""
    stream = FakeStream(tip=1_000_000)
    assert _verify_forward(stream, 900_000, 500_000) <= 1_000_000
    assert _verify_forward(stream, 900_000, 500_000) > 900_000, "недопрыгнул"


def test_verify_forward_returns_base_when_nothing_alive():
    class Dead(FakeStream):
        def batch(self, seq, count=100):
            return []

    stream = Dead(tip=0)
    assert _verify_forward(stream, 500, 1000) == 500


class LagCache(ScanCache):
    pass


def _lag_stream(now, lag_s, count=100):
    """Поток, отдающий матчи заданной давности, затем пустоту."""
    base = 1_000_000
    batches = {base: [_timed(base + i, i, int(now - lag_s))
                      for i in range(count)],
               base + count: []}
    stream = FakeStream(tip=10**9, batches=batches)
    return stream, base


def test_scan_jumps_forward_when_lagging(monkeypatch):
    """Читая подряд, сканер отстаёт на две трети скорости потока."""
    import collector.ranks as ranks_mod
    now = 1_700_000_000.0
    monkeypatch.setattr(ranks_mod.time, "time", lambda: now)
    stream, base = _lag_stream(now, lag_s=48 * 3600)
    cache = LagCache({})
    cache.cursor = base
    funnel, _ = scan(cache, stream, matches=100)
    assert funnel["отставание, ч"] == 48
    assert funnel.get("прыжок вперёд", 0) > 0, "сканер не догоняет край"
    assert cache.cursor > base + 100, "курсор не сдвинут вперёд"


def test_scan_never_jumps_past_the_live_edge(monkeypatch):
    """Перелёт оставил бы курсор в пустоте НАВСЕГДА.

    Каждый следующий проход читал бы ноль матчей и не имел бы ни
    малейшего повода вернуться назад: пустой ответ неотличим от «догнали
    край». Поэтому прыжок обязан быть подтверждён живым ответом.
    """
    import collector.ranks as ranks_mod
    now = 1_700_000_000.0
    monkeypatch.setattr(ranks_mod.time, "time", lambda: now)
    base, tip = 1_000_000, 1_200_000
    # Отставание двое суток при темпе 10 seq/с даёт расчётный прыжок
    # ~1.37 млн — заведомо дальше живого края.
    stream = FakeStream(tip=tip, batches={
        base: [_timed(base + i * 10, i, int(now - 48 * 3600))
               for i in range(100)],
        base + 990: [],
    })
    cache = LagCache({})
    cache.cursor = base
    scan(cache, stream, matches=100)
    assert cache.cursor > base, "не сдвинулся вовсе"
    assert cache.cursor <= tip, f"курсор улетел в пустоту: {cache.cursor}"
    assert stream.batch(cache.cursor, count=1), "курсор в мёртвой зоне"


def test_scan_does_not_jump_when_close_to_the_edge(monkeypatch):
    """Прыгать без нужды значит пропускать матчи впустую.

    Отставание намеренно ВЫШЕ целевого (час), но ниже порога догона
    (шесть часов): проверяется именно порог, а не то, что расчётный
    прыжок вышел отрицательным.
    """
    import collector.ranks as ranks_mod
    now = 1_700_000_000.0
    monkeypatch.setattr(ranks_mod.time, "time", lambda: now)
    stream, base = _lag_stream(now, lag_s=2 * 3600)
    cache = LagCache({})
    cache.cursor = base
    funnel, _ = scan(cache, stream, matches=100)
    assert "прыжок вперёд" not in funnel
    assert cache.cursor == base + 100


def test_scan_reports_lag_even_without_jump(monkeypatch):
    """Отставание — главный симптом будущего простоя, оно должно быть видно."""
    import collector.ranks as ranks_mod
    now = 1_700_000_000.0
    monkeypatch.setattr(ranks_mod.time, "time", lambda: now)
    stream, base = _lag_stream(now, lag_s=3600)
    cache = LagCache({})
    cache.cursor = base
    funnel, _ = scan(cache, stream, matches=100)
    assert funnel["отставание, ч"] == 1


# -- отчёт по очереди -------------------------------------------------------------

class ReportQueue:
    def __init__(self, states, prec, sample=()):
        self._states, self._prec, self._sample = states, prec, list(sample)

    def stats(self):
        return self._states

    def precision(self, min_rank=80):
        return self._prec

    def precision_sample(self, limit=20):
        return self._sample


def _prec(done=0, known=0, immortal=0, avg=0, ranks=0):
    return {"скачано": done, "факт известен": known,
            "из них immortal": immortal, "средний ранг": avg,
            "рангов на матч": ranks}


def test_queue_report_shows_precision_when_matches_downloaded():
    """Спринт 129 добавлял этот блок скриптовой правкой по шаблону с
    неверным отступом: str.replace молча не сработал, и главная поставка
    спринта уехала отсутствующей. Тесты покрывали precision() на уровне
    БД и ни одного — вывод команды. Этот тест закрывает ровно ту дыру.
    """
    q = ReportQueue({"done": 199, "new": 470},
                    _prec(done=199, known=180, immortal=171, avg=79, ranks=8))
    out = queue_report(q)
    assert "точность правила отбора" in out
    assert "95.0%" in out, out


def test_queue_report_hides_precision_until_something_downloaded():
    q = ReportQueue({"new": 12}, _prec())
    out = queue_report(q)
    assert "точность" not in out


def test_queue_report_says_when_truth_was_never_recorded():
    """199 матчей скачаны до миграции 009 — знаменатель ноль, не деление."""
    q = ReportQueue({"done": 199}, _prec(done=199))
    out = queue_report(q)
    assert "факт ещё не собран" in out
    assert "%" not in out.split("факт ещё не собран")[0].split(
        "точность правила отбора")[-1]


def test_queue_report_marks_sample_as_prediction_not_truth():
    """Столбец avg_rank в выборке — предсказание кэша; путать их дорого."""
    q = ReportQueue({"done": 1}, _prec(done=1), [(123, 2, 80)])
    out = queue_report(q)
    assert "ПРЕДСКАЗАНИЕ" in out


# -- цикл ------------------------------------------------------------------------

class _Args:
    command = "scan"
    interval = 5
    matches = 10
    budget = 10
    resolver = None
    ttl_days = 30
    limit = None


def test_loop_survives_a_failing_run(monkeypatch):
    """Сбой одного прогона не должен останавливать конвейер.

    Реплейный путь однажды простоял 82 часа при зелёном pgrep — цена
    молча умершего фонового процесса измеряется неделями данных.
    """
    import collector.ranks as ranks_mod
    runs = []

    def flaky(cache, dsn, args):
        runs.append(len(runs))
        if len(runs) == 1:
            raise RuntimeError("сеть отвалилась")
        if len(runs) >= 3:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(ranks_mod, "_run_once", flaky)
    monkeypatch.setattr(ranks_mod.time, "sleep", lambda s: None)
    assert ranks_mod._loop(object(), "dsn", _Args()) == 0
    assert len(runs) == 3, "цикл умер на первом же сбое"


def test_loop_reconnects_after_database_failure(monkeypatch):
    """Инцидент 2026-07-20: мёртвое соединение с PG после его рестарта."""
    import collector.ranks as ranks_mod
    reconnects = []
    runs = []

    def flaky(cache, dsn, args):
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("connection already closed")
        raise KeyboardInterrupt

    monkeypatch.setattr(ranks_mod, "_run_once", flaky)
    monkeypatch.setattr(ranks_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(ranks_mod, "_reconnect",
                        lambda c, d: reconnects.append(1) or c)
    ranks_mod._loop(object(), "dsn", _Args())
    assert reconnects, "соединение не пересоздано"


def test_loop_subtracts_run_time_from_the_interval(monkeypatch):
    """Интервал — период, а не пауза сверху: иначе долгий прогон растягивает
    цикл вдвое и темп сбора молча падает."""
    import collector.ranks as ranks_mod
    slept = []
    clock = {"t": 0.0}

    def run(cache, dsn, args):
        clock["t"] += 4.0
        if slept:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(ranks_mod, "_run_once", run)
    monkeypatch.setattr(ranks_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ranks_mod.time, "sleep", lambda s: slept.append(s))
    ranks_mod._loop(object(), "dsn", _Args())
    assert slept and abs(slept[0] - 1.0) < 1e-6, slept


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
