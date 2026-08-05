"""Тесты источника STRATZ (спринт 79).

Проверяется то, где легко тихо испортить датасет: перевод поминутных
рядов в строки витрины, накопление убийств, отказ писать чужой id патча
в колонку patch и поведение при ошибках GraphQL/квоты.
"""
import pathlib
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.sources import Shard  # noqa: E402
from collector.sources.stratz import (GAME_MODE_NAMES,  # noqa: E402
                                      LOBBY_NAMES, StratzError,
                                      StratzTimelineSource, cumulative,
                                      enum_id, match_passes, parse_patch_dates,
                                      networth_totals, patch_at,
                                      stratz_rank, timeline_rows)
from collector.sources.stratz import draft_row as stratz_draft  # noqa: E402


def _match(mid=7000000000, minutes=4, win=True):
    return {
        "id": mid,
        "didRadiantWin": win,
        "durationSeconds": 1800,
        "gameMode": 1,
        "lobbyType": 7,
        "startDateTime": 1785000000,
        "rank": 80,
        "radiantNetworthLeads": [0, 100, 250, 400][:minutes],
        "radiantExperienceLeads": [0, 80, 200, 350][:minutes],
        "radiantKills": [0, 1, 2, 0][:minutes],
        "direKills": [0, 0, 1, 1][:minutes],
    }


# -- ряды ---------------------------------------------------------------------

def test_cumulative_accumulates_per_minute_series():
    assert cumulative([0, 1, 2, 0]) == [0, 1, 3, 3]


def test_cumulative_respects_explicit_flag():
    """Вид ряда задаётся явно, а не угадывается: [0,0,1,1] одинаково
    правдоподобен и как поминутный, и как накопительный, и эвристика на
    таком ряду ошибается молча."""
    assert cumulative([0, 0, 1, 1]) == [0, 0, 1, 2]
    assert cumulative([0, 0, 1, 1], already_cumulative=True) == [0, 0, 1, 1]


def test_cumulative_handles_empty():
    assert cumulative([]) == []


def test_timeline_rows_grid_and_values():
    rows = timeline_rows(_match())
    assert [r["game_time"] for r in rows] == [60, 120, 180]
    assert [r["networth_diff"] for r in rows] == [100, 250, 400]
    assert [r["xp_diff"] for r in rows] == [80, 200, 350]
    # убийства накопительные, как в остальных источниках
    assert [r["kills_radiant"] for r in rows] == [1, 3, 3]
    assert [r["kills_dire"] for r in rows] == [0, 1, 2]
    assert all(r["radiant_win"] == 1 for r in rows)


def test_double_accumulation_is_flagged(caplog):
    """Если ряд на деле приходит накопительным, двойное накопление даёт
    нереальные числа — это должно быть видно в логе, а не разойтись
    молча по всему датасету."""
    m = _match(minutes=4)
    m["radiantKills"] = [50, 60, 70, 80]
    with caplog.at_level("WARNING"):
        timeline_rows(m)
    assert "STRATZ_KILLS_CUMULATIVE" in caplog.text


def test_kills_are_integers_not_nan():
    """kills_* — UInt16 в витрине: NaN туда не вставить, вставка упала бы
    целиком и матч потерялся бы молча."""
    m = _match()
    m["radiantKills"] = []          # ряда нет вовсе
    m["direKills"] = []
    rows = timeline_rows(m)
    assert rows
    for r in rows:
        assert isinstance(r["kills_radiant"], int)
        assert isinstance(r["kills_dire"], int)


def test_replay_only_features_are_nan():
    """position_advance/alive_diff есть только в реплее — ноль был бы
    ложным сигналом («бой в центре карты»), нужен именно пропуск."""
    import math
    r = timeline_rows(_match())[0]
    assert math.isnan(r["position_advance"])
    assert math.isnan(r["alive_diff"])


def test_short_timeline_yields_nothing():
    assert timeline_rows(_match(minutes=1)) == []


# -- фильтр -------------------------------------------------------------------

def test_match_passes_ok():
    assert match_passes(_match(), 900, 59, patch=60)[0]


def test_match_rejected_by_turbo_and_short_and_patch():
    turbo = _match(); turbo["gameMode"] = 23
    assert match_passes(turbo, 900, None) == (False, "mode")
    short = _match(); short["durationSeconds"] = 300
    assert match_passes(short, 900, None) == (False, "short")
    # min_patch и patch — оба в нумерации OpenDota. Раньше слева стоял
    # gameVersionId STRATZ, то есть сравнивались номера из разных шкал.
    assert match_passes(_match(), 900, 60, patch=59) == (False, "old-patch")
    empty = _match(); empty["radiantNetworthLeads"] = []
    assert match_passes(empty, 900, None) == (False, "no-timeline")


def test_enum_accepts_names_and_numbers():
    """STRATZ отдаёт lobbyType/gameMode строками-энумами ('UNRANKED'), а
    OpenDota — числами. Инцидент 2026-07-31: int('UNRANKED') ронял цикл."""
    assert enum_id("UNRANKED", LOBBY_NAMES) == 0
    assert enum_id("RANKED", LOBBY_NAMES) == 7
    assert enum_id(7, LOBBY_NAMES) == 7
    assert enum_id("7", LOBBY_NAMES) == 7
    assert enum_id("TURBO", GAME_MODE_NAMES) == 23
    assert enum_id("ALL_PICK_RANKED", GAME_MODE_NAMES) == 22


def test_unknown_enum_value_rejects_match_and_warns(caplog):
    """Незнакомое имя не должно молча протащить матч в обучающую выборку:
    отсеиваем, но пишем в лог, чтобы энум можно было дополнить."""
    with caplog.at_level("WARNING"):
        assert enum_id("SOME_NEW_MODE", GAME_MODE_NAMES) == -1
    assert "SOME_NEW_MODE" in caplog.text
    assert enum_id(None, LOBBY_NAMES) == -1


def test_match_passes_with_string_enums():
    """Тот самый матч, на котором падал коллектор."""
    m = _match()
    m["lobbyType"] = "RANKED"
    m["gameMode"] = "ALL_PICK_RANKED"
    assert match_passes(m, 900, None)[0]

    turbo = _match()
    turbo["lobbyType"] = "RANKED"
    turbo["gameMode"] = "TURBO"
    assert match_passes(turbo, 900, None) == (False, "mode")

    unranked_lobby = _match()
    unranked_lobby["lobbyType"] = "BATTLE_CUP"
    assert match_passes(unranked_lobby, 900, None) == (False, "lobby")


def test_bad_match_does_not_break_cycle(monkeypatch):
    """Один неразбираемый матч не должен обнулять весь проход: раньше
    ValueError из match_passes ронял цикл, а с ним и половину притока."""
    broken = _match(50)
    broken["durationSeconds"] = "не число"
    good = _match(52)
    src = make_source(monkeypatch, {50: broken, 52: good}, [50, 52])
    got = list(src.fetch_new())
    assert [t.match_id for t in got] == [52]


def test_pro_mode_skips_lobby_and_mode_checks():
    """Лиги играются в турнирном лобби и Captains Mode — обычные фильтры
    отбросили бы всю эталонную выборку."""
    m = _match(); m["lobbyType"] = 2; m["gameMode"] = 2
    assert match_passes(m, 900, None, pro=True)[0]


# -- источник -----------------------------------------------------------------

PATCHES = [(1742770259, 59), (1774300000, 60)]   # 7.40 и 7.41


def make_source(monkeypatch, matches, candidates, mode="public",
                patches=None, gql_hook=None):
    src = StratzTimelineSource(token="t", limit_per_cycle=10, mode=mode,
                               api_delay_s=0, min_patch=None)
    src._patches = PATCHES if patches is None else patches
    # Пустой справочник в бою означает «прочитать не удалось» и влечёт
    # повторное чтение. Здесь он задан явно, но часы всё равно взводим:
    # иначе тест с пустым справочником получил бы лишний вызов OpenDota.
    src._patch_map_at = time.monotonic()

    def fake_gql(query, variables=None):
        if gql_hook:
            gql_hook(query, variables)
        mid = (variables or {}).get("id")
        return {"match": matches.get(mid)}

    monkeypatch.setattr(src, "_gql", fake_gql)
    monkeypatch.setattr(src, "_opendota",
                        lambda path, **kw: [{"match_id": m} for m in candidates])
    return src


def test_fetch_new_yields_timeline_matches(monkeypatch):
    src = make_source(monkeypatch, {11: _match(11)}, [11])
    got = list(src.fetch_new())
    assert [t.match_id for t in got] == [11]
    assert got[0].tier == "Premium"
    assert len(got[0].rows) == 3


def test_pro_mode_uses_professional_tier(monkeypatch):
    m = _match(12); m["lobbyType"] = 2
    src = make_source(monkeypatch, {12: m}, [12], mode="pro")
    assert list(src.fetch_new())[0].tier == "Professional"


def test_skip_predicate_prevents_api_call(monkeypatch):
    """Дедуп срабатывает ДО запроса в STRATZ — лимит не тратится на то,
    что уже собрано другим источником."""
    asked = []
    src = make_source(monkeypatch, {13: _match(13)}, [13],
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    assert list(src.fetch_new(skip=lambda mid: mid == 13)) == []
    assert asked == []


def test_shard_filters_before_api_call(monkeypatch):
    asked = []
    src = make_source(monkeypatch, {14: _match(14), 15: _match(15)}, [14, 15],
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    src._shard = Shard(shard_id=1, count=2)
    got = list(src.fetch_new())
    assert [t.match_id for t in got] == [15]      # только нечётный
    assert asked == [15]


def test_rejected_match_not_asked_twice(monkeypatch):
    """Отвергнутый фильтром матч кэшируется: вердикт не изменится, а
    лимит STRATZ — главный дефицит."""
    turbo = _match(16); turbo["gameMode"] = 23
    asked = []
    src = make_source(monkeypatch, {16: turbo}, [16],
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    assert list(src.fetch_new()) == []
    assert list(src.fetch_new()) == []
    assert asked == [16]                          # второй цикл не спрашивал


def test_limit_per_cycle_respected(monkeypatch):
    matches = {i: _match(i) for i in (20, 22, 24, 26)}
    src = make_source(monkeypatch, matches, list(matches))
    src._limit = 2
    assert len(list(src.fetch_new())) == 2


def test_schema_error_aborts_cycle(monkeypatch):
    """Ошибка схемы GraphQL повторится на каждом матче — цикл обрывается,
    а не выжигает лимит одинаковыми битыми запросами."""
    asked = []

    def boom(query, variables=None):
        asked.append((variables or {}).get("id"))
        raise StratzError("Cannot query field 'radiantKills'")

    src = make_source(monkeypatch, {}, [30, 31, 32])
    monkeypatch.setattr(src, "_gql", boom)
    assert list(src.fetch_new()) == []
    assert asked == [30]                          # оборвались на первом


def test_rate_limit_propagates(monkeypatch):
    """429/401 не глотаются: их обрабатывает демон (пауза либо явная
    ошибка токена), иначе коллектор молча крутил бы пустые циклы."""
    resp = requests.Response()
    resp.status_code = 429

    def limited(query, variables=None):
        raise requests.HTTPError(response=resp)

    src = make_source(monkeypatch, {}, [40])
    monkeypatch.setattr(src, "_gql", limited)
    with pytest.raises(requests.HTTPError):
        list(src.fetch_new())


def test_runner_writes_stratz_rows_and_own_feature_version(monkeypatch):
    """Сквозняк через раннер: строки STRATZ ложатся в витрину, целочисленные
    колонки не получают nan, а feature_version отличается от opendota-json —
    иначе два источника с разным набором фич не разделить в анализе."""
    from collector import timeline_runner
    from collector.sources.opendota_timeline import TimelineMatch

    inserted = {}

    class FakeCur:
        def __init__(self, store): self._s = store
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, params=None):
            self._s.setdefault("sql", []).append((q.split()[0], params))
        def fetchone(self): return None

    class FakeDB:
        closed = False
        def __init__(self, store): self._s = store
        def cursor(self): return FakeCur(self._s)
        def close(self): pass

    pg_store = {}
    monkeypatch.setattr(timeline_runner.psycopg, "connect",
                        lambda dsn, autocommit: FakeDB(pg_store))

    def fake_post(url, params=None, data=None, headers=None, timeout=None):
        inserted.setdefault("bodies", []).append(data.decode())
        inserted["query"] = params["query"]
        class R:
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr(timeline_runner.requests, "post", fake_post)

    class OneShot:
        name = "stratz_timeline"
        feature_version = "stratz-graphql@1"
        def fetch_new(self, skip=None):
            yield TimelineMatch(match_id=77, tier="Premium",
                                rows=timeline_rows(_match(77)),
                                source_cursor="77", patch=57)

    coll = timeline_runner.TimelineCollector(
        timeline_runner.TimelineConfig(), OneShot())
    assert coll.collect_once() == 1

    lines = inserted["bodies"][0].strip().split("\n")
    assert len(lines) == 3
    cols = dict(zip(timeline_runner.MTF_COLUMNS, lines[0].split("\t")))
    assert cols["match_id"] == "77" and cols["game_time"] == "60"
    assert cols["feature_version"] == "stratz-graphql@1"
    assert cols["patch"] == "57"
    # UInt16/UInt8-колонки: только целые, иначе вставка упала бы целиком
    for c in ("kills_radiant", "kills_dire", "radiant_win", "patch"):
        assert cols[c].lstrip("-").isdigit(), (c, cols[c])
    # Float32-колонки реплейного происхождения — честный пропуск
    for c in ("position_advance", "alive_diff", "towers_diff", "rax_diff"):
        assert cols[c] == "nan"


def test_empty_token_rejected():
    """Пустой токен — конфигурационная ошибка: лучше упасть на старте,
    чем крутить циклы, получая 401 на каждый матч."""
    with pytest.raises(ValueError, match="STRATZ_API_TOKEN"):
        StratzTimelineSource(token="")


def test_candidates_paginate_beyond_first_page(monkeypatch):
    """Одной страницы не хватает: шард делит её пополам, SourceSplit —
    ещё пополам, дедуп добивает остаток. Из-за этого цикл собирал 4–13
    матчей при лимите 40, упираясь не в квоту STRATZ, а в кандидатов."""
    pages = {
        None: [{"match_id": m} for m in range(9000, 8990, -1)],
        8991: [{"match_id": m} for m in range(8990, 8980, -1)],
        8981: [],
    }
    calls = []

    src = StratzTimelineSource(token="t", api_delay_s=0)

    def fake_opendota(path, **params):
        key = params.get("less_than_match_id")
        calls.append(key)
        return pages.get(key, [])

    monkeypatch.setattr(src, "_opendota", fake_opendota)
    got = list(src._candidates())
    assert len(got) == 20, got
    assert calls == [None, 8991, 8981]


def test_candidates_stop_when_api_ignores_cursor(monkeypatch):
    """Если API отдаёт ту же страницу, листать бессмысленно — иначе цикл
    жёг бы вызовы, гоняя одни и те же id по кругу."""
    src = StratzTimelineSource(token="t", api_delay_s=0)
    calls = []

    def fake_opendota(path, **params):
        calls.append(params.get("less_than_match_id"))
        return [{"match_id": 777}]

    monkeypatch.setattr(src, "_opendota", fake_opendota)
    assert list(src._candidates()) == [777]     # без повторов
    assert len(calls) == 2                       # вторая страница и стоп


# -- временные отказы против постоянных (спринт 87) ---------------------------

def test_missing_match_is_retried_not_buried(monkeypatch):
    """STRATZ парсит матч с задержкой, а кандидаты берутся с вершины
    листинга — самые свежие. Матч, которого у STRATZ ещё нет, обязан
    попробоваться снова: прежняя версия хоронила его навсегда, и
    источник душил сам себя (инцидент 2026-08-03 — сбор упал с 25 до 3
    матчей за цикл при израсходованных 8% суточной квоты)."""
    asked = []
    ready = {}
    src = make_source(monkeypatch, ready, [17],
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    assert list(src.fetch_new()) == []            # STRATZ ещё не знает матч
    ready[17] = _match(17)                        # распарсился между циклами
    assert [t.match_id for t in src.fetch_new()] == [17]
    assert asked == [17, 17]


def test_missing_match_gives_up_after_attempts(monkeypatch):
    """Бесконечно спрашивать тоже нельзя: матч, который STRATZ не
    распарсит никогда, обязан осесть в постоянном кэше отказов."""
    asked = []
    src = make_source(monkeypatch, {}, [18],
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    src._retry_attempts = 3
    for _ in range(5):
        assert list(src.fetch_new()) == []
    assert asked == [18, 18, 18]                  # ровно retry_attempts


def test_empty_timeline_is_transient_too(monkeypatch):
    """Матч у STRATZ есть, а рядов ещё нет — он в очереди на парсинг.
    Та же временная причина, что и отсутствие самого матча."""
    m = _match(19)
    m["radiantNetworthLeads"] = []
    m["radiantExperienceLeads"] = []
    store = {19: m}
    src = make_source(monkeypatch, store, [19])
    assert list(src.fetch_new()) == []
    store[19] = _match(19)
    assert [t.match_id for t in src.fetch_new()] == [19]


def test_permanent_filter_is_not_retried(monkeypatch):
    """Обратная сторона: турбо не станет рейтинговым матчем никогда, и
    повторный вызов — чистая трата квоты."""
    turbo = _match(21); turbo["gameMode"] = 23
    asked = []
    src = make_source(monkeypatch, {21: turbo}, [21],
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    for _ in range(4):
        assert list(src.fetch_new()) == []
    assert asked == [21]


def test_detail_budget_caps_calls_per_cycle(monkeypatch):
    """Лимит цикла считает УСПЕХИ, а вызов тратится и на промах. Без
    отдельного потолка цикл с высокой долей промахов перебирал бы все
    1000 кандидатов и выжигал часовую квоту."""
    asked = []
    src = make_source(monkeypatch, {}, list(range(100, 200)),
                      gql_hook=lambda q, v: asked.append((v or {}).get("id")))
    src._detail_budget = 7
    assert list(src.fetch_new()) == []
    assert len(asked) == 7


def test_default_detail_budget_leaves_room_for_misses(monkeypatch):
    """Дефолт — кратно лимиту, а не равен ему: иначе первый же десяток
    несозревших матчей съедал бы бюджет и цикл возвращал ноль."""
    src = StratzTimelineSource(token="t", limit_per_cycle=25, api_delay_s=0)
    assert src._detail_budget > 25


# -- патч по дате матча (спринт 89) -------------------------------------------

OD_PATCHES = [
    {"id": 58, "name": "7.39", "date": "2025-05-22T23:36:01.602Z"},
    {"id": 59, "name": "7.40", "date": "2025-12-16T00:50:40.281Z"},
    {"id": 60, "name": "7.41", "date": "2026-03-24T00:50:59.580Z"},
]


def test_parse_patch_dates_sorted_epochs():
    got = parse_patch_dates(OD_PATCHES)
    assert [pid for _ts, pid in got] == [58, 59, 60]
    assert got[0][0] < got[1][0] < got[2][0]


def test_parse_patch_dates_skips_broken_entries():
    """Битая запись не должна ронять весь справочник: без даты патч
    просто не участвует в сопоставлении."""
    got = parse_patch_dates(OD_PATCHES + [{"id": 61, "name": "7.42"},
                                          {"id": None, "date": "2026-01-01Z"},
                                          {"id": 62, "date": "не дата"}])
    assert [pid for _ts, pid in got] == [58, 59, 60]


def test_patch_at_picks_last_released_before_match():
    p = parse_patch_dates(OD_PATCHES)
    import datetime as dt
    after_741 = int(dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc).timestamp())
    between = int(dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc).timestamp())
    before_all = int(dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    assert patch_at(after_741, p) == 60
    assert patch_at(between, p) == 59
    assert patch_at(before_all, p) == 0


def test_patch_at_unknown_is_zero_not_guess():
    """Ноль — «неизвестен», и patch_weights его НЕ штрафует. Любое
    угаданное число здесь означало бы тихий даунвейт (см. ниже)."""
    assert patch_at(0, parse_patch_dates(OD_PATCHES)) == 0
    assert patch_at(1785000000, []) == 0


def test_match_on_patch_unknown_to_stratz_gets_current(monkeypatch):
    """Регресс 2026-08-03, ради которого всё переписано.

    Справочник версий STRATZ заканчивался на 7.40b, хотя игра четыре
    месяца как на 7.41; матчам на 7.41 STRATZ проставлял последнюю
    известную ему версию. Перевод ПО ИМЕНИ давал таким матчам patch=59
    при актуальном 60 — и patch_weights умножал их вес на 0.4, то есть
    треть датасета получала штраф за несуществующее устаревание. Ноль
    («неизвестен») был бы безобиднее, а верная дата — правильнее всего.
    """
    src = StratzTimelineSource(token="t", api_delay_s=0)
    monkeypatch.setattr(src, "_opendota", lambda path, **kw: OD_PATCHES)
    import datetime as dt
    started = int(dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc).timestamp())
    assert src._patch_of({"startDateTime": started}) == 60


def test_patch_survives_unreadable_reference(monkeypatch):
    """Недоступный constants не рушит сбор: витрина наполняется, patch
    честно остаётся нулём."""
    src = StratzTimelineSource(token="t", api_delay_s=0)

    def boom(*a, **kw):
        raise requests.ConnectionError("нет сети")

    monkeypatch.setattr(src, "_opendota", boom)
    assert src._patch_of({"startDateTime": 1785000000}) == 0


def test_patch_reference_reread_is_rate_limited(monkeypatch):
    """Повтор чтения не должен стать вызовом на каждый матч."""
    src = StratzTimelineSource(token="t", api_delay_s=0)
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise requests.ConnectionError("нет сети")

    monkeypatch.setattr(src, "_opendota", boom)
    for _ in range(50):
        assert src._patch_of({"startDateTime": 1785000000}) == 0
    assert calls["n"] == 1


def test_patch_reference_recovers_after_failure(monkeypatch):
    """Пустой справочник — повод перечитать, а не приговор на весь срок
    жизни процесса."""
    src = StratzTimelineSource(token="t", api_delay_s=0)
    calls = {"n": 0}

    def od(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("нет сети")
        return OD_PATCHES

    monkeypatch.setattr(src, "_opendota", od)
    assert src._patch_of({"startDateTime": 1785000000}) == 0
    src._patch_map_at = -10**9
    assert src._patch_of({"startDateTime": 1785000000}) == 60


def test_collected_match_carries_patch(monkeypatch):
    """Сквозняк: патч доезжает до TimelineMatch, а не теряется в цикле."""
    src = make_source(monkeypatch, {31: _match(31)}, [31])
    got = list(src.fetch_new())
    assert got[0].patch == 60


# -- ранговый фильтр (спринт 94) ----------------------------------------------

def test_rank_reads_full_scale_not_bracket():
    """Из четырёх полей-кандидатов годится `rank`: `averageRank` на живых
    матчах пуст, а `bracket` — это rank // 10, то есть тир без звезды.
    По bracket порог 80 превратился бы в «тир 8», и Divine 5 (75) прошёл
    бы наравне с Immortal."""
    assert stratz_rank({"rank": 80, "bracket": 8}) == 80
    assert stratz_rank({"rank": 64}) == 64


def test_rank_falls_back_and_defaults_to_zero():
    assert stratz_rank({"rank": None, "actualRank": 71}) == 71
    assert stratz_rank({"rank": None, "actualRank": None}) == 0
    assert stratz_rank({}) == 0
    assert stratz_rank({"rank": "не число"}) == 0


def test_low_rank_match_is_rejected():
    """Замер 2026-08-04: из четырёх свежесобранных матчей два оказались
    rank 44 (Legend 4) и 64 (Ancient 4) — под ярлыком tier='Premium',
    который у OpenDota означает 80+. Половина «высокоранговой» выборки
    высокоранговой не была."""
    low = _match(); low["rank"] = 44
    assert match_passes(low, 900, None, min_rank=80) == (False, "low-rank")
    mid = _match(); mid["rank"] = 64
    assert match_passes(mid, 900, None, min_rank=80) == (False, "low-rank")


def test_high_rank_match_passes():
    high = _match(); high["rank"] = 80
    assert match_passes(high, 900, None, min_rank=80)[0]


def test_unknown_rank_is_rejected_like_opendota():
    """OpenDota отбрасывает матч, когда рангов меньше пяти
    («ranks-unknown»). Взять такой матч «на всякий случай» значило бы
    вернуть ту же смесь популяций, ради устранения которой фильтр и
    вводится."""
    unknown = _match(); unknown["rank"] = None
    assert match_passes(unknown, 900, None, min_rank=80) == (False,
                                                            "rank-unknown")


def test_rank_filter_off_by_default():
    """min_rank=0 отключает проверку: про-режим и старые конфигурации не
    должны внезапно потерять весь приток."""
    low = _match(); low["rank"] = 20
    assert match_passes(low, 900, None, min_rank=0)[0]


def test_pro_mode_ignores_rank():
    """У про-игроков ранг скрыт или не показателен — на эталонной
    выборке фильтр отсёк бы всё."""
    pro = _match(); pro["lobbyType"] = 2; pro["rank"] = None
    assert match_passes(pro, 900, None, pro=True, min_rank=80)[0]


def test_collected_match_carries_rank(monkeypatch):
    """Сквозняк: ранг доезжает до TimelineMatch, иначе колонка витрины
    осталась бы нулевой и проверить популяцию было бы нечем."""
    src = make_source(monkeypatch, {41: _match(41)}, [41])
    got = list(src.fetch_new())
    assert got[0].avg_rank == 80


# -- отступ от вершины листинга (спринт 95) -----------------------------------

def _listing_source(monkeypatch, ids, skip):
    src = StratzTimelineSource(token="t", api_delay_s=0, skip_freshest=skip)
    pages = [ids[i:i + 100] for i in range(0, len(ids), 100)] or [[]]
    calls = {"n": 0}

    def fake_od(path, **kw):
        i = calls["n"]
        calls["n"] += 1
        return [{"match_id": m} for m in (pages[i] if i < len(pages) else [])]

    monkeypatch.setattr(src, "_opendota", fake_od)
    return src


def test_skip_freshest_drops_top_of_listing(monkeypatch):
    """Матч с вершины листинга у STRATZ обычно ещё без поминутных рядов:
    вызов уходит впустую, и бюджет цикла выгорает на матчах, которых у
    него нет. В логе 2026-08-04 — «собрано 1 из 421 кандидатов, вызовов
    100, ждут парсинга: 87»."""
    ids = list(range(9000, 8800, -1))          # 200 штук, новые первыми
    src = _listing_source(monkeypatch, ids, skip=150)
    got = list(src._candidates())
    assert got == ids[150:]


def test_skip_zero_keeps_old_behaviour(monkeypatch):
    ids = list(range(9000, 8900, -1))
    src = _listing_source(monkeypatch, ids, skip=0)
    assert list(src._candidates()) == ids


def test_skip_larger_than_listing_yields_nothing(monkeypatch):
    """Отступ больше листинга не должен ронять цикл — просто пустой
    проход, следующий цикл возьмёт своё."""
    ids = list(range(9000, 8950, -1))
    src = _listing_source(monkeypatch, ids, skip=500)
    assert list(src._candidates()) == []


def test_skip_counts_across_pages(monkeypatch):
    """Отступ считается по всему листингу, а не по каждой странице:
    иначе с пагинацией он резал бы вершину каждой страницы, а не одну
    вершину окна."""
    ids = list(range(9000, 8700, -1))          # три страницы
    src = _listing_source(monkeypatch, ids, skip=120)
    got = list(src._candidates())
    assert got[0] == ids[120]
    assert len(got) == len(ids) - 120


# -- нетворс и драфт из ответа STRATZ (спринт 100) ----------------------------

def _players(radiant_ids=(1, 2, 3, 4, 5), dire_ids=(6, 7, 8, 9, 10),
             networth=None):
    out = []
    for i, hid in enumerate(radiant_ids):
        out.append({"playerSlot": i, "heroId": hid,
                    "stats": {"networthPerMinute": networth or []}})
    for i, hid in enumerate(dire_ids):
        out.append({"playerSlot": 128 + i, "heroId": hid,
                    "stats": {"networthPerMinute": networth or []}})
    return out


def test_networth_totals_sums_all_players():
    """radiantNetworthLeads — РАЗНОСТЬ, суммы из неё не получить. Без
    поминутного нетворса игроков networth_total оставался NaN, а с ним
    отсутствовал networth_rel — у 46% датасета."""
    m = {"players": _players(networth=[0, 100, 250])}
    assert networth_totals(m, 3) == [0.0, 1000.0, 2500.0]


def test_networth_totals_tolerates_short_and_missing_series():
    m = {"players": [{"stats": {"networthPerMinute": [10, 20]}},
                     {"stats": {"networthPerMinute": [1]}},
                     {"stats": {}}]}
    assert networth_totals(m, 3) == [11.0, 20.0, 0.0]


def test_networth_totals_empty_when_no_player_stats():
    """Пустой список означает NaN в витрине — честнее нуля, который читался
    бы как «в матче не заработали золота»."""
    assert networth_totals({"players": [{"stats": {}}]}, 3) == []
    assert networth_totals({}, 3) == []


def test_timeline_rows_carry_networth_total():
    m = _match()
    m["players"] = _players(networth=[0, 500, 900, 1200])
    rows = timeline_rows(m)
    assert rows[0]["networth_total"] == 5000.0     # минута 1, десять игроков


def test_timeline_rows_keep_nan_without_player_stats():
    import math as _m
    rows = timeline_rows(_match())          # фикстура без players
    assert _m.isnan(rows[0]["networth_total"])


def test_draft_row_splits_sides_by_player_slot():
    """Составы у STRATZ лежат в том же ответе, который мы и так просим.
    До спринта 100 MatchDraft наполнялся только матчами OpenDota."""
    m = {"id": 77, "didRadiantWin": True, "players": _players()}
    d = stratz_draft(m)
    assert d is not None
    assert len(d["radiant_heroes"]) == 5 and len(d["dire_heroes"]) == 5
    assert d["radiant_win"] == 1 and d["source"] == "stratz"
    assert d["bans"] == [] and d["first_pick_team"] == 0


def test_draft_row_rejects_unknown_hero():
    """Неизвестный герой означает битый или устаревший словарь, а не
    новую сторону: писать такой драфт в обучение нельзя."""
    m = {"id": 77, "players": _players(radiant_ids=(1, 2, 3, 4, 99999))}
    assert stratz_draft(m) is None


def test_draft_row_rejects_incomplete_roster():
    m = {"id": 77, "players": _players()[:8]}
    assert stratz_draft(m) is None


def test_collected_match_carries_draft(monkeypatch):
    m = _match(51)
    m["players"] = _players()
    src = make_source(monkeypatch, {51: m}, [51])
    got = list(src.fetch_new())
    assert got[0].draft is not None
    assert got[0].draft["match_id"] == 51


# -- отступ в единицах match_id, самонастраивающийся (спринт 114) -------------

def _lag_src(monkeypatch, ids, ready=None, **kw):
    """Источник с листингом из готовых id и подконтрольным отступом."""
    src = make_source(monkeypatch, ready if ready is not None else {}, ids)
    for k, v in kw.items():
        setattr(src, f"_{k}", v)
    return src


def test_lag_measured_in_match_ids_not_entries():
    """Отступ «в записях» означал то полчаса, то три: плотность листинга
    зависит от того, сколько матчей успел распарсить OpenDota, а не от
    того, сколько времени было у STRATZ. Замер 2026-08-05: id растут
    ~789/мин, 100 записей /parsedMatches укладываются в ~30 000 id."""
    src = OBJ = None
    from collector.sources.stratz import StratzTimelineSource
    src = StratzTimelineSource.__new__(StratzTimelineSource)
    src._id_lag, src._id_lag_min, src._id_lag_max = 0, 1000, 400_000
    # Пустой цикл при нулевых вызовах обязан ужимать отступ, а не залипать.
    src._id_lag = 50_000
    src._adapt_lag(calls=0, misses=0)
    assert src._id_lag == 25_000


def test_lag_grows_fast_on_misses():
    """Каждый цикл с промахами — сожжённая квота. Растём ×1.5."""
    from collector.sources.stratz import StratzTimelineSource
    src = StratzTimelineSource.__new__(StratzTimelineSource)
    src._id_lag, src._id_lag_min, src._id_lag_max = 40_000, 1000, 400_000
    src._adapt_lag(calls=100, misses=90)
    assert src._id_lag == 60_000


def test_lag_shrinks_slowly_when_clean():
    """Слишком глубокий отступ матчей не теряет (листинг — движущееся
    окно), он лишь добавляет дубликатов. Ошибка вверх дешевле, поэтому
    убываем медленно (×0.9)."""
    from collector.sources.stratz import StratzTimelineSource
    src = StratzTimelineSource.__new__(StratzTimelineSource)
    src._id_lag, src._id_lag_min, src._id_lag_max = 100_000, 1000, 400_000
    src._adapt_lag(calls=100, misses=2)
    assert src._id_lag == 90_000


def test_lag_holds_in_the_middle_band():
    """Между 10% и 50% промахов не дёргаемся: STRATZ парсит не мгновенно
    и не строго по порядку, единичные промахи неустранимы, и гнаться за
    нулём значит уползать всё глубже."""
    from collector.sources.stratz import StratzTimelineSource
    src = StratzTimelineSource.__new__(StratzTimelineSource)
    src._id_lag, src._id_lag_min, src._id_lag_max = 100_000, 1000, 400_000
    src._adapt_lag(calls=100, misses=30)
    assert src._id_lag == 100_000


def test_lag_is_clamped():
    from collector.sources.stratz import StratzTimelineSource
    src = StratzTimelineSource.__new__(StratzTimelineSource)
    src._id_lag, src._id_lag_min, src._id_lag_max = 390_000, 1000, 400_000
    src._adapt_lag(calls=10, misses=10)
    assert src._id_lag == 400_000
    # Снизу зажим тоже держит: с минимума дальше не уползаем.
    src._id_lag = 1000
    src._adapt_lag(calls=10, misses=0)
    assert src._id_lag == 1000


def test_fresh_candidates_are_skipped_by_id_distance(monkeypatch):
    """Кандидаты ближе отступа к вершине не отдаются вовсе."""
    ids = [900_000, 880_000, 800_000, 700_000]
    src = _lag_src(monkeypatch, ids, id_lag=100_000, skip_freshest=0)
    got = list(src._candidates())
    assert 900_000 not in got and 880_000 not in got
    assert got == [800_000, 700_000]


def test_lag_never_starves_the_source(monkeypatch):
    """САМОЕ ВАЖНОЕ. Отступ отсчитывается от вершины листинга, поэтому
    при большом значении до кандидатов надо пролистать несколько страниц.
    Если листинг короче отступа (мало распаршенных матчей, ночной провал,
    лимит страниц), цикл не сделал бы ни одного вызова — а подстройка,
    которой не на чем учиться, оставила бы отступ прежним, и источник
    замолчал бы НАВСЕГДА. Ровно так он душил себя кэшем отказов до
    спринта 87.
    """
    ids = [900_000, 899_000, 898_000]          # весь листинг уже отступа
    src = _lag_src(monkeypatch, ids, id_lag=400_000, skip_freshest=0)
    got = list(src._candidates())
    assert got, "отступ съел весь листинг и источник замолчал"
    assert set(got) <= set(ids)


def test_zero_call_cycle_shrinks_the_lag():
    """Вторая половина той же защиты: если вызовов не было, «нет
    промахов» выглядит как здоровье. Отступ обязан ужиматься сам."""
    from collector.sources.stratz import StratzTimelineSource
    src = StratzTimelineSource.__new__(StratzTimelineSource)
    src._id_lag, src._id_lag_min, src._id_lag_max = 200_000, 1000, 400_000
    src._adapt_lag(calls=0, misses=0)
    assert src._id_lag == 100_000
