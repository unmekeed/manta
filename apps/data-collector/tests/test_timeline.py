"""Тесты JSON-таймлайн источника (opendota_timeline) и его раннера."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources.opendota_timeline import (OpenDotaTimelineSource,
                                                 match_passes, timeline_rows)


def _parsed_match(mid=100, minutes=20, radiant_win=True):
    """Минимальный JSON распаршенного матча OpenDota."""
    return {
        "match_id": mid,
        "radiant_win": radiant_win,
        "duration": minutes * 60,
        "lobby_type": 7,
        "game_mode": 22,
        "patch": 60,
        "radiant_gold_adv": [i * 100 for i in range(minutes + 1)],
        "radiant_xp_adv": [i * 120 for i in range(minutes + 1)],
        "players": [
            # Radiant (slot < 128): убийства на 90с и 400с
            {"player_slot": 0, "rank_tier": 80,
             "kills_log": [{"time": 90}, {"time": 400}]},
            {"player_slot": 1, "rank_tier": 81, "kills_log": []},
            # Dire: убийство на 200с
            {"player_slot": 128, "rank_tier": 80,
             "kills_log": [{"time": 200}]},
            {"player_slot": 129, "rank_tier": 79, "kills_log": []},
            {"player_slot": 130, "rank_tier": 80, "kills_log": []},
        ],
    }


def test_timeline_rows_grid_and_kills():
    m = _parsed_match(minutes=20)
    # gold_t игроков → networth_total по минутам (сумма обеих команд)
    for p in m["players"]:
        p["gold_t"] = [i * 200 for i in range(21)]
    rows = timeline_rows(m)
    # сетка минут: 60..1200 (нулевая пропущена)
    assert [r["game_time"] for r in rows] == [i * 60 for i in range(1, 21)]
    assert rows[0]["networth_diff"] == 100 and rows[-1]["networth_diff"] == 2000
    # 5 игроков в фикстуре × 200·i золота
    assert rows[0]["networth_total"] == 5 * 200.0
    assert rows[-1]["networth_total"] == 5 * 200.0 * 20
    # убийства накопительно: к 60с — 0; к 120с — 1 (Radiant, 90с);
    # к 240с — 1R + 1D (200с); к 420с — 2R
    by_t = {r["game_time"]: r for r in rows}
    assert (by_t[60]["kills_radiant"], by_t[60]["kills_dire"]) == (0, 0)
    assert (by_t[120]["kills_radiant"], by_t[120]["kills_dire"]) == (1, 0)
    assert (by_t[240]["kills_radiant"], by_t[240]["kills_dire"]) == (1, 1)
    assert by_t[420]["kills_radiant"] == 2
    # позиций в JSON нет → NaN (не 0!)
    assert all(math.isnan(r["position_advance"]) for r in rows)
    assert all(r["radiant_win"] == 1 for r in rows)


def test_match_passes_filters():
    ok, _ = match_passes(_parsed_match(), 80, 900, 60)
    assert ok
    turbo = _parsed_match(); turbo["game_mode"] = 23
    assert match_passes(turbo, 80, 900, 60) == (False, "mode")
    short = _parsed_match(minutes=5)
    assert match_passes(short, 80, 900, 60) == (False, "short")
    low = _parsed_match()
    for p in low["players"]:
        p["rank_tier"] = 50
    assert match_passes(low, 80, 900, 60) == (False, "low-rank")
    old = _parsed_match(); old["patch"] = 55
    assert match_passes(old, 80, 900, 60) == (False, "old-patch")
    unparsed = _parsed_match(); unparsed["radiant_gold_adv"] = None
    assert match_passes(unparsed, 80, 900, 60) == (False, "no-timeline")


def test_fetch_new_skips_collected_before_detail_call(monkeypatch):
    """Дедуп срабатывает ДО дорогого вызова /matches/{id} — бюджет API
    не тратится на уже собранные матчи."""
    src = OpenDotaTimelineSource(limit_per_cycle=2, min_patch=60,
                                 api_delay_s=0)
    detail_calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_get(path, **params):
        if path == "parsedMatches":
            return FakeResp([{"match_id": m} for m in (5, 4, 3, 2, 1)])
        assert path.startswith("matches/")
        mid = int(path.split("/")[1])
        detail_calls.append(mid)
        return FakeResp(_parsed_match(mid=mid))

    monkeypatch.setattr(src, "_get", fake_get)
    got = list(src.fetch_new(skip=lambda mid: mid in {5, 3}))
    assert [t.match_id for t in got] == [4, 2]        # 5 и 3 пропущены
    assert detail_calls == [4, 2]                     # без лишних вызовов
    assert got[0].tier == "Premium"
    assert len(got[0].rows) == 20


def test_runner_inserts_and_marks(monkeypatch):
    """Раннер: вставка строк в CH (nan как текст) + отметка в PG."""
    from collector import timeline_runner
    from collector.sources.opendota_timeline import TimelineMatch

    inserted = {}

    class FakeCur:
        def __init__(self, store): self._s = store
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, params=None):
            self._s.setdefault("sql", []).append((q.split()[0], params))
            self._q = q
        def fetchone(self):
            return None  # ничего не собрано

    class FakeDB:
        closed = False
        def __init__(self, store): self._s = store
        def cursor(self): return FakeCur(self._s)
        def close(self): pass

    pg_store = {}
    monkeypatch.setattr(timeline_runner.psycopg, "connect",
                        lambda dsn, autocommit: FakeDB(pg_store))

    def fake_post(url, params=None, data=None, headers=None, timeout=None):
        inserted["query"] = params["query"]
        inserted["body"] = data.decode()
        class R:
            def raise_for_status(self): pass
        return R()

    monkeypatch.setattr(timeline_runner.requests, "post", fake_post)

    class OneShotSource:
        name = "opendota_timeline"
        def fetch_new(self, skip=None):
            rows = timeline_rows(_parsed_match(mid=42, minutes=3))
            yield TimelineMatch(match_id=42, tier="Premium", rows=rows,
                                source_cursor="42")

    coll = timeline_runner.TimelineCollector(
        timeline_runner.TimelineConfig(), OneShotSource())
    assert coll.collect_once() == 1
    assert "MatchTimelineFeatures" in inserted["query"]
    lines = inserted["body"].strip().split("\n")
    assert len(lines) == 3
    first = lines[0].split("\t")
    assert first[0] == "42" and first[1] == "60"
    # Ровно шесть пропусков, и каждый осмыслен: position_advance и
    # alive_diff существуют только в реплее; networth_total,
    # unspent_gold_diff и buyback_availability — фикстура без gold_t;
    # vision_coverage_diff — в фикстуре нет вардов с координатами.
    # Проверка на ТОЧНОЕ число намеренна: лишний nan означает потерянную
    # фичу, недостающий — ноль вместо пропуска, то есть ложный сигнал.
    # Спринт 185: +5 — готовых рядов OpenDota (lh/dn/урон/лечение/
    # стаки) в фикстуре нет, и это ПРОПУСК, а не «никто не фармил».
    # Спринт 186: +4 — драк в фикстуре нет, и это тоже
    # ПРОПУСК, а не «драк не случилось».
    # Спринт 187: +13 — состава в фикстуре нет (герои её игроков
    # не заданы), и это пропуск, а не «составы симметричны».
    assert lines[0].count("nan") == 28
    assert "opendota-json@3" in lines[0]
    # PG: INSERT в CollectedMatches и CollectorCursor
    kinds = [k for k, _ in pg_store["sql"]]
    assert kinds.count("INSERT") == 2


def test_timeline_rows_building_diffs_from_objectives():
    """towers_diff/rax_diff из objectives: снесённое goodguys-здание — очко
    Dire (−1), badguys — очко Radiant (+1), накопительно по минутам."""
    m = _parsed_match(minutes=10)
    m["objectives"] = [
        {"type": "building_kill", "time": 130,
         "key": "npc_dota_badguys_tower1_mid"},     # Radiant снёс: +1
        {"type": "building_kill", "time": 250,
         "key": "npc_dota_goodguys_tower1_top"},    # Dire снёс: -1
        {"type": "building_kill", "time": 400,
         "key": "npc_dota_badguys_melee_rax_bot"},  # ракс Radiant'ом: +1
        {"type": "CHAT_MESSAGE_FIRSTBLOOD", "time": 90},  # не здание
    ]
    rows = timeline_rows(m)
    by_t = {r["game_time"]: r for r in rows}
    assert by_t[120]["towers_diff"] == 0.0
    assert by_t[180]["towers_diff"] == 1.0
    assert by_t[300]["towers_diff"] == 0.0     # +1 и −1
    assert by_t[360]["rax_diff"] == 0.0
    assert by_t[420]["rax_diff"] == 1.0
    # alive недоступен из JSON
    import math as _m
    assert all(_m.isnan(r["alive_diff"]) for r in rows)


def test_pro_mode_filters_and_tier(monkeypatch):
    """pro-режим: кандидаты из /proMatches, tier=Professional, фильтр без
    рангов/лобби (CM в турнирном лобби проходит), короткие отсекаются."""
    src = OpenDotaTimelineSource(limit_per_cycle=2, min_patch=60,
                                 api_delay_s=0, mode="pro")
    assert src.name == "opendota_timeline_pro"

    def _pro_match(mid, minutes=25):
        m = _parsed_match(mid=mid, minutes=minutes)
        m["lobby_type"] = 1      # турнирное лобби
        m["game_mode"] = 2       # Captains Mode
        for p in m["players"]:
            p.pop("rank_tier", None)   # у про-игроков ранги скрыты
        return m

    calls = []

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def json(self): return self._p

    def fake_get(path, **params):
        calls.append(path)
        if path == "proMatches":
            return FakeResp([{"match_id": m} for m in (9, 8, 7)])
        mid = int(path.split("/")[1])
        return FakeResp(_pro_match(mid, minutes=25 if mid != 8 else 5))

    monkeypatch.setattr(src, "_get", fake_get)
    got = list(src.fetch_new(skip=lambda mid: False))
    assert "proMatches" in calls
    assert [t.match_id for t in got] == [9, 7]     # 8 отсечён (5 минут)
    assert all(t.tier == "Professional" for t in got)


def test_pro_match_passes_ignores_rank_and_lobby():
    m = _parsed_match()
    m["lobby_type"] = 1
    m["game_mode"] = 2
    for p in m["players"]:
        p.pop("rank_tier", None)
    # public-фильтр отверг бы (lobby), pro — пропускает
    assert match_passes(m, 80, 900, 60)[0] is False
    assert match_passes(m, 80, 900, 60, pro=True) == (True, "ok")


def test_fetch_new_429_aborts_cycle(monkeypatch):
    """Исчерпанная квота (429) обрывает цикл: остальные кандидаты дали бы
    те же 429, тратя остаток лимита и время (runbook «витрина не растёт»)."""
    import pytest
    import requests

    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0)
    detail_calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_get(path, **params):
        if path == "parsedMatches":
            return FakeResp([{"match_id": m} for m in (3, 2, 1)])
        mid = int(path.split("/")[1])
        detail_calls.append(mid)
        if mid == 2:
            resp = requests.Response()
            resp.status_code = 429
            raise requests.HTTPError(response=resp)
        return FakeResp(_parsed_match(mid=mid))

    monkeypatch.setattr(src, "_get", fake_get)
    with pytest.raises(requests.HTTPError):
        list(src.fetch_new())
    assert detail_calls == [3, 2]    # матч 1 не запрашивался


def test_fetch_new_transient_error_skips_match(monkeypatch):
    """Не-429 сбой одного матча (таймаут, 5xx) не роняет цикл — пропуск."""
    import requests

    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0)

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_get(path, **params):
        if path == "parsedMatches":
            if params.get("less_than_match_id"):
                return FakeResp([])          # старее ничего нет
            return FakeResp([{"match_id": m} for m in (3, 2, 1)])
        mid = int(path.split("/")[1])
        if mid == 2:
            raise requests.ConnectionError("boom")
        return FakeResp(_parsed_match(mid=mid))

    monkeypatch.setattr(src, "_get", fake_get)
    got = list(src.fetch_new())
    assert [t.match_id for t in got] == [3, 1]


def test_with_api_key_mixed_into_params():
    from collector.sources import with_api_key
    assert with_api_key(None, None) == {}
    assert with_api_key(None, "K") == {"api_key": "K"}
    assert with_api_key({"a": "1"}, "K") == {"a": "1", "api_key": "K"}
    base = {"a": "1"}
    assert with_api_key(base, "K") is not base   # исходный dict не мутируем


def test_rejected_candidates_cached_across_cycles(monkeypatch):
    """Отфильтрованный кандидат платит detail-вызов один раз: второй цикл
    берёт вердикт из кэша (бюджет анонимного тарифа)."""
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0)
    detail_calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_get(path, **params):
        if path == "parsedMatches":
            if params.get("less_than_match_id"):
                return FakeResp([])
            return FakeResp([{"match_id": m} for m in (3, 2, 1)])
        mid = int(path.split("/")[1])
        detail_calls.append(mid)
        m = _parsed_match(mid=mid)
        if mid == 2:                      # low-rank — постоянный отказ
            for p in m["players"]:
                p["rank_tier"] = 10
        return FakeResp(m)

    monkeypatch.setattr(src, "_get", fake_get)
    assert [t.match_id for t in src.fetch_new()] == [3, 1]
    assert detail_calls == [3, 2, 1]
    detail_calls.clear()
    # Второй цикл: 3 и 1 отсечёт дедуп-предикат, 2 — кэш отказов.
    assert list(src.fetch_new(skip=lambda mid: mid in {3, 1})) == []
    assert detail_calls == []


def test_detail_budget_caps_cycle(monkeypatch):
    """Бюджет detail-вызовов обрывает цикл, даже если лимит матчей не добран."""
    src = OpenDotaTimelineSource(limit_per_cycle=10, min_patch=60,
                                 api_delay_s=0, detail_budget=2)
    detail_calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    def fake_get(path, **params):
        if path == "parsedMatches":
            return FakeResp([{"match_id": m} for m in (5, 4, 3, 2, 1)])
        mid = int(path.split("/")[1])
        detail_calls.append(mid)
        return FakeResp(_parsed_match(mid=mid))

    monkeypatch.setattr(src, "_get", fake_get)
    got = list(src.fetch_new())
    assert [t.match_id for t in got] == [5, 4]
    assert detail_calls == [5, 4]        # 3, 2, 1 не запрашивались


def test_ensure_db_reconnects_after_server_restart(monkeypatch):
    """Рестарт Postgres (docker restart) убивает соединение: раннер обязан
    пересоздать его сам, а не падать каждый цикл до ручного pkill
    (инцидент 2026-07-20)."""
    import psycopg as psycopg_mod

    from collector import timeline_runner
    from collector.timeline_runner import TimelineCollector, TimelineConfig

    class DeadCur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, *a, **k):
            raise psycopg_mod.OperationalError("server closed the connection")

    class LiveCur:
        def __init__(self, log): self._log = log
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, q, params=None): self._log.append(q.split()[0])
        def fetchone(self): return None

    class FakeConn:
        def __init__(self, dead, log):
            self.dead, self.closed, self._log = dead, False, log
        def cursor(self):
            return DeadCur() if self.dead else LiveCur(self._log)
        def close(self): self.closed = True

    queries: list[str] = []
    conns = [FakeConn(dead=True, log=queries),   # старое, умершее
             FakeConn(dead=False, log=queries)]  # новое после reconnect
    monkeypatch.setattr(timeline_runner.psycopg, "connect",
                        lambda dsn, autocommit: conns.pop(0))

    class EmptySource:
        name = "opendota_timeline"
        def fetch_new(self, skip=None):
            return iter(())

    coll = TimelineCollector(TimelineConfig(), EmptySource())
    assert coll.collect_once() == 0          # цикл выжил
    assert conns == []                       # переподключение состоялось
    assert coll._db.dead is False
    # Живое соединение повторно не пересоздаётся.
    assert coll.collect_once() == 0
    assert queries.count("SELECT") >= 1


def test_patch_floor_keeps_previous_patch(monkeypatch):
    """В день выхода патча матчей на нём почти нет, и планка «строго
    текущий» обнуляет приток на сутки-двое — так случилось 2026-07-31
    с выходом 7.41. Принимаем текущий и PATCH_LAG предыдущих."""
    import os
    from collector.sources.opendota_timeline import OpenDotaTimelineSource

    class FakeResp:
        def json(self):
            return [{"id": 56, "name": "7.39"}, {"id": 57, "name": "7.40"},
                    {"id": 58, "name": "7.41"}]

    src = OpenDotaTimelineSource(api_delay_s=0)
    monkeypatch.setattr(src, "_get", lambda path, **kw: FakeResp())

    os.environ.pop("PATCH_LAG", None)
    assert src._latest_patch() == 57          # 7.41 и 7.40 проходят

    os.environ["PATCH_LAG"] = "0"             # прежнее строгое поведение
    try:
        assert src._latest_patch() == 58
    finally:
        os.environ.pop("PATCH_LAG", None)


def test_patch_floor_never_negative(monkeypatch):
    class FakeResp:
        def json(self):
            return [{"id": 0, "name": "old"}]

    from collector.sources.opendota_timeline import OpenDotaTimelineSource
    src = OpenDotaTimelineSource(api_delay_s=0)
    monkeypatch.setattr(src, "_get", lambda path, **kw: FakeResp())
    assert src._latest_patch() == 0


# -- лог цикла называет причину отказа (спринт 104) ---------------------------

class _R:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _cycle_line(caplog):
    """Единственная строка отчёта цикла из лога."""
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("цикл ")]
    assert len(lines) == 1, f"ожидалась одна строка отчёта, найдено: {lines}"
    return lines[0]


def test_cycle_log_names_each_skip_reason(monkeypatch, caplog):
    """Инцидент 2026-08-04: режим league отдавал «processed=0», и по этому
    нулю нельзя было отличить «лиги не дали матчей» от «всё уже собрано»
    от «всё отсеяно фильтром». Отказы фильтра уходили в logger.debug, а
    отсев по шарду/доле/дедупу/кэшу не считался вовсе.
    """
    from collector.sources import Shard

    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0, shard=Shard(1, 2))

    def fake_get(path, **params):
        if path == "parsedMatches":
            if params.get("less_than_match_id"):
                return _R([])
            return _R([{"match_id": m} for m in (11, 12, 13, 15)])
        mid = int(path.split("/")[1])
        m = _parsed_match(mid=mid)
        if mid == 13:                      # low-rank — отказ фильтра
            for p in m["players"]:
                p["rank_tier"] = 10
        return _R(m)

    monkeypatch.setattr(src, "_get", fake_get)
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        got = [t.match_id for t in src.fetch_new(skip=lambda mid: mid == 11)]

    assert got == [15]
    line = _cycle_line(caplog)
    assert "из 4 кандидатов" in line
    assert "чужой шард: 1" in line          # 12 — чётный, шард 1 из 2
    assert "дубликат: 1" in line            # 11 отсеян предикатом дедупа
    assert "фильтр: 1" in line              # 13 — low-rank
    assert "· low-rank: 1" in line          # и конкретная причина фильтра


def test_league_cycle_reports_zero_leagues(monkeypatch, caplog):
    """Ноль лиг обязан ПЕЧАТАТЬСЯ. Нулевые счётчики из отчёта убраны для
    читаемости, и без исключения для league-ключей молчание о лигах было
    бы неотличимо от исправной работы — та самая подмена «нет данных» на
    «всё хорошо»."""
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0, mode="league")
    monkeypatch.setattr(src, "_get", lambda path, **p: _R([]))
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        assert list(src.fetch_new()) == []
    line = _cycle_line(caplog)
    assert "лиг найдено: 0" in line
    assert "матчей в лигах: 0" in line


def test_league_cycle_distinguishes_empty_leagues(monkeypatch, caplog):
    """Лиги нашлись, но матчей не отдали — другая поломка, другой ремонт."""
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0, mode="league")

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 7},
                       {"match_id": 2, "leagueid": 8}])
        if path == "leagues":
            return _R([])                  # каталог пуст — только активные
        return _R([])                      # leagues/{id}/matches — пусто

    monkeypatch.setattr(src, "_get", fake_get)
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        assert list(src.fetch_new()) == []
    line = _cycle_line(caplog)
    assert "лиг найдено: 2" in line
    assert "лиг опрошено: 2" in line
    assert "лиг пустых: 2" in line
    assert "матчей в лигах: 0" in line


def test_league_cycle_reports_filter_breakdown(monkeypatch, caplog):
    """Матчи из лиг есть, но все отсеяны — видно, ЧЕМ именно."""
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0, mode="league")

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 7}])
        if path == "leagues":
            return _R([])
        if path.startswith("leagues/"):
            return _R([{"match_id": 41}, {"match_id": 42}])
        m = _parsed_match(mid=int(path.split("/")[1]))
        m["patch"] = 50                    # старее планки min_patch=60
        return _R(m)

    monkeypatch.setattr(src, "_get", fake_get)
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        assert list(src.fetch_new()) == []
    line = _cycle_line(caplog)
    assert "матчей в лигах: 2" in line
    assert "· old-patch: 2" in line


def test_report_survives_quota_abort(monkeypatch, caplog):
    """429 обрывает цикл исключением. Это САМЫЙ частый обрыв, и молчать о
    нём нельзя — иначе исчерпание квоты выглядит как отсутствие матчей."""
    import requests

    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0)

    def fake_get(path, **params):
        if path == "parsedMatches":
            return _R([{"match_id": 5}])
        resp = requests.Response()
        resp.status_code = 429
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(src, "_get", fake_get)
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        try:
            list(src.fetch_new())
        except requests.HTTPError:
            pass
    assert "из 1 кандидатов" in _cycle_line(caplog)


def test_report_survives_early_stop(monkeypatch, caplog):
    """Потребитель вправе бросить генератор, не досмотрев до конца."""
    src = OpenDotaTimelineSource(limit_per_cycle=50, min_patch=60,
                                 api_delay_s=0)

    def fake_get(path, **params):
        if path == "parsedMatches":
            if params.get("less_than_match_id"):
                return _R([])
            return _R([{"match_id": m} for m in (9, 8, 7)])
        return _R(_parsed_match(mid=int(path.split("/")[1])))

    monkeypatch.setattr(src, "_get", fake_get)
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        gen = src.fetch_new()
        next(gen)
        gen.close()
    assert "собрано 1" in _cycle_line(caplog)


def test_public_cycle_hides_league_counters(monkeypatch, caplog):
    """Обратная сторона исключения для нулей: в public-режиме league-ключи
    не нужны и только зашумляют строку."""
    src = OpenDotaTimelineSource(limit_per_cycle=5, min_patch=60,
                                 api_delay_s=0)
    monkeypatch.setattr(src, "_get", lambda path, **p: _R([]))
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        assert list(src.fetch_new()) == []
    assert "лиг найдено" not in _cycle_line(caplog)


# -- лиги берутся из каталога, а не из окна /proMatches (спринт 106) ----------

def _league_src(**kw):
    kw.setdefault("limit_per_cycle", 50)
    kw.setdefault("min_patch", 60)
    kw.setdefault("api_delay_s", 0)
    kw.setdefault("mode", "league")
    return OpenDotaTimelineSource(**kw)


def test_catalog_leagues_extend_beyond_active_window():
    """Смысл спринта 96 — обойти окно /proMatches. Но `_active_leagues`
    брал лиги ИЗ ЭТОГО ЖЕ окна: 4 лиги по ~50 матчей, потолок ~200, и он
    выбирался за сутки (замер 2026-08-05: собрано 1 из 205, дубликатов
    89). В каталоге /leagues таких лиг 2681."""
    src = _league_src(league_batch=3)
    calls = []

    def fake_get(path, **params):
        calls.append(path)
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 7}])
        if path == "leagues":
            return _R([{"leagueid": i, "tier": "professional"}
                       for i in (100, 101, 102, 103)]
                      + [{"leagueid": 999, "tier": "excluded"}])
        return _R([])

    src._get = fake_get
    got = src._league_batch()
    assert got[0] == 7, "активная лига обязана опрашиваться всегда"
    assert len(got) == 1 + 3, f"пачка каталога не добрана: {got}"
    assert 999 not in got, "лига чужого тира просочилась"


def test_catalog_walked_newest_first():
    """Порядок обхода — убывающий leagueid, то есть ход НАЗАД ПО ВРЕМЕНИ
    от текущих турниров: id выдаются по мере создания лиги.

    Первая версия шла в порядке ответа API, и первые же восемь лиг
    оказались турнирами прошлых лет (замер 2026-08-05: «старее окна: 422,
    лиг выдохлось: 8, собрано 0»). Полный круг по 2681 лиге пачками по 8
    занял бы две недели, и всё это время источник давал бы ноль.
    """
    src = _league_src(league_batch=3)

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 19900}])
        if path == "leagues":
            return _R([{"leagueid": i, "tier": "premium"}
                       for i in (11000, 19800, 15000, 19850, 12000)])
        return _R([])

    src._get = fake_get
    assert src._league_batch() == [19900, 19850, 19800, 15000]


def test_legacy_league_block_is_cut_off():
    """11 лиг с id 60000+ — это The International 2013 и современники,
    другое пространство id. Без потолка обход начинался бы с них.

    Потолок берётся ОТ АКТИВНЫХ лиг, а не константой: константа протухнет
    через год и молча выкинет весь свежий диапазон — а выглядело бы это
    как «в каталоге ничего нет»."""
    src = _league_src(league_batch=5)

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 20030}])
        if path == "leagues":
            return _R([{"leagueid": i, "tier": "premium"}
                       for i in (65019, 60001, 19917, 15000)])
        return _R([])

    src._get = fake_get
    got = src._league_batch()
    assert 65019 not in got and 60001 not in got, got
    assert got == [20030, 19917, 15000]


def test_no_active_leagues_means_no_blind_walk():
    """Без активных лиг ориентира нет, а обходить 2681 запись вслепую —
    это гадание за счёт квоты."""
    src = _league_src()

    def fake_get(path, **params):
        if path == "leagues":
            return _R([{"leagueid": 15000, "tier": "premium"}])
        return _R([])

    src._get = fake_get
    assert src._league_batch() == []


def test_old_matches_dropped_before_detail_call():
    """Отсев по дате обязан происходить ДО /matches/{id}: именно он
    делает обход тысяч завершённых турниров дешёвым. Если он съедет в
    match_passes, каждый древний матч будет стоить detail-вызова."""
    import time as _t

    src = _league_src(league_max_age_days=30)
    details = []

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 7}])
        if path == "leagues":
            return _R([])
        if path.startswith("leagues/"):
            return _R([{"match_id": 41, "start_time": int(_t.time()) - 5,
                        "duration": 2000},
                       {"match_id": 43, "start_time": int(_t.time()) - 400 * 86400,
                        "duration": 2000}])
        details.append(path)
        return _R(_parsed_match(mid=int(path.split("/")[1])))

    src._get = fake_get
    got = [t.match_id for t in src.fetch_new()]
    assert got == [41]
    assert details == ["matches/41"], f"древний матч стоил вызова: {details}"


def test_missing_fields_are_not_a_verdict():
    """Пустое start_time — это «дата неизвестна», а не «матч древний»;
    пустое duration — «неизвестна», а не «ноль секунд». Первая версия
    отсева считала отсутствие поля нулём и молча выбрасывала кандидата —
    ровно та подмена, из-за которой спринт 99 потерял все покупки."""
    src = _league_src()

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 7}])
        if path == "leagues":
            return _R([])
        if path.startswith("leagues/"):
            return _R([{"match_id": 41}])      # ни даты, ни длительности
        return _R(_parsed_match(mid=41))

    src._get = fake_get
    assert [t.match_id for t in src.fetch_new()] == [41]


def test_exhausted_league_is_not_polled_again(caplog):
    """Турнир без единого свежего матча закончился и новых не даст.
    Помнить это надо — иначе каждый цикл платит вызов за одни и те же
    мёртвые лиги, а их тысячи."""
    import time as _t

    src = _league_src(league_batch=2)

    def fake_get(path, **params):
        if path == "proMatches":
            return _R([{"match_id": 1, "leagueid": 19000}])
        if path == "leagues":
            return _R([{"leagueid": i, "tier": "premium"}
                       for i in (18000, 17000)])
        return _R([{"match_id": 900, "start_time": int(_t.time()) - 400 * 86400,
                    "duration": 2000}])

    src._get = fake_get
    with caplog.at_level("INFO", logger="collector.opendota_timeline"):
        assert list(src.fetch_new()) == []
    assert "лиг выдохлось: 3" in _cycle_line(caplog)
    assert src._dead_leagues == {19000, 18000, 17000}
    # Фронт съезжает вглубь сам: выбывшие больше не опрашиваются, и
    # прокрутка позиции для этого не нужна.
    assert src._league_batch() == [], "мёртвые лиги снова в опросе"


def test_catalog_refresh_forgets_dead_leagues():
    """Вердикт «свежих матчей нет» верен на момент проверки. Держать его
    вечно — это ошибка STRATZ из спринта 87, где транзиентная причина
    попала в постоянный кэш и задушила источник."""
    src = _league_src()
    src._dead_leagues = {10, 11}
    src._get = lambda path, **p: _R(
        [{"leagueid": 10, "tier": "premium"}] if path == "leagues" else [])
    src._catalog_leagues()
    assert src._dead_leagues == set()
