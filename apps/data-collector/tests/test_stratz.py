"""Тесты источника STRATZ (спринт 79).

Проверяется то, где легко тихо испортить датасет: перевод поминутных
рядов в строки витрины, накопление убийств, отказ писать чужой id патча
в колонку patch и поведение при ошибках GraphQL/квоты.
"""
import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from collector.sources import Shard  # noqa: E402
from collector.sources.stratz import (StratzError,  # noqa: E402
                                      StratzTimelineSource, cumulative,
                                      match_passes, timeline_rows)


def _match(mid=7000000000, minutes=4, win=True):
    return {
        "id": mid,
        "didRadiantWin": win,
        "durationSeconds": 1800,
        "gameMode": 1,
        "lobbyType": 7,
        "gameVersionId": 180,
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
    assert match_passes(_match(), 900, 180)[0]


def test_match_rejected_by_turbo_and_short_and_patch():
    turbo = _match(); turbo["gameMode"] = 23
    assert match_passes(turbo, 900, None) == (False, "mode")
    short = _match(); short["durationSeconds"] = 300
    assert match_passes(short, 900, None) == (False, "short")
    old = _match(); old["gameVersionId"] = 100
    assert match_passes(old, 900, 180) == (False, "old-patch")
    empty = _match(); empty["radiantNetworthLeads"] = []
    assert match_passes(empty, 900, None) == (False, "no-timeline")


def test_pro_mode_skips_lobby_and_mode_checks():
    """Лиги играются в турнирном лобби и Captains Mode — обычные фильтры
    отбросили бы всю эталонную выборку."""
    m = _match(); m["lobbyType"] = 2; m["gameMode"] = 2
    assert match_passes(m, 900, None, pro=True)[0]


# -- источник -----------------------------------------------------------------

def make_source(monkeypatch, matches, candidates, mode="public",
                patch_map=None, gql_hook=None):
    src = StratzTimelineSource(token="t", limit_per_cycle=10, mode=mode,
                               api_delay_s=0, min_patch=None)
    src._patch_map = {} if patch_map is None else patch_map

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


# -- патчи --------------------------------------------------------------------

def test_patch_map_translates_by_version_name(monkeypatch):
    """gameVersionId STRATZ != patch id OpenDota; связывает их имя версии."""
    src = StratzTimelineSource(token="t", api_delay_s=0)
    monkeypatch.setattr(src, "_gql", lambda q, v=None: {
        "constants": {"gameVersions": [{"id": 180, "name": "7.39"},
                                       {"id": 179, "name": "7.38"}]}})
    monkeypatch.setattr(src, "_opendota", lambda path, **kw: [
        {"id": 57, "name": "7.39"}, {"id": 56, "name": "7.38"}])
    assert src._patch_of({"gameVersionId": 180}) == 57
    assert src._patch_of({"gameVersionId": 179}) == 56


def test_unknown_patch_is_zero_not_foreign_id(monkeypatch):
    """Неизвестная версия → 0 («неизвестен»). Записать сюда id STRATZ
    значило бы молча испортить даунвейт старых патчей при обучении."""
    src = StratzTimelineSource(token="t", api_delay_s=0)
    monkeypatch.setattr(src, "_gql", lambda q, v=None: {
        "constants": {"gameVersions": [{"id": 180, "name": "7.39"}]}})
    monkeypatch.setattr(src, "_opendota",
                        lambda path, **kw: [{"id": 57, "name": "7.39"}])
    assert src._patch_of({"gameVersionId": 999}) == 0


def test_patch_map_failure_does_not_break_collection(monkeypatch):
    """Недоступные constants не должны останавливать сбор — витрина
    наполняется, patch честно остаётся нулём."""
    src = StratzTimelineSource(token="t", api_delay_s=0)

    def boom(*a, **kw):
        raise requests.ConnectionError("нет сети")

    monkeypatch.setattr(src, "_gql", boom)
    monkeypatch.setattr(src, "_opendota", boom)
    assert src._patch_of({"gameVersionId": 180}) == 0


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
