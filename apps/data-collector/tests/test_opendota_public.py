"""Тесты OpenDotaPublicSource: патч-фильтр, качество матчей, skip без salt."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources import opendota_public
from collector.sources.opendota_public import OpenDotaPublicSource


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def ranked(mid, **over):
    row = {"match_id": mid, "lobby_type": 7, "game_mode": 22, "duration": 2000}
    row.update(over)
    return row


def make_source(monkeypatch, public_rows, details, patches=None, **kwargs):
    calls = {"public": 0}

    def fake_get(url, params=None, **kw):
        if url.endswith("/constants/patch"):
            return FakeResp(patches or [{"id": 60, "name": "7.41"}])
        if url.endswith("/publicMatches"):
            calls["public"] += 1
            if params and "less_than_match_id" in params:
                ceiling = int(params["less_than_match_id"])
                return FakeResp([r for r in public_rows
                                 if r["match_id"] < ceiling])
            return FakeResp(public_rows)
        for mid, d in details.items():
            if url.endswith(f"/matches/{mid}"):
                return FakeResp(d) if d is not None else FakeResp(None, 404)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(opendota_public.requests, "get", fake_get)
    monkeypatch.setattr(opendota_public.time, "sleep", lambda s: None)
    return OpenDotaPublicSource(lag_matches=100, **kwargs)


def test_filters_lobby_mode_duration_and_patch(monkeypatch):
    rows = [
        ranked(10),
        ranked(11, game_mode=23),          # турбо — вон
        ranked(12, lobby_type=0),          # не ранкед — вон
        ranked(13, duration=500),          # слишком короткий — вон
        ranked(14),                        # старый патч (детали ниже)
        ranked(500),                       # свежее окна (ceiling=400) — вон
    ]
    src = make_source(
        monkeypatch, rows,
        details={
            10: {"patch": 60, "replay_url": "http://r/10.dem.bz2"},
            14: {"patch": 59, "replay_url": "http://r/14.dem.bz2"},
        },
        limit_per_cycle=10)
    refs = list(src.fetch_new(None))
    assert [r.match_id for r in refs] == [10]
    assert refs[0].tier == "Premium"


def test_skips_matches_without_replay(monkeypatch):
    """Без salt матч пропускается, но цикл продолжается (паблик ≠ pro)."""
    rows = [ranked(10), ranked(11), ranked(1000)]  # 1000 задаёт ceiling=900
    src = make_source(
        monkeypatch, rows,
        details={
            10: {"patch": 60, "replay_url": None},
            11: {"patch": 60, "replay_url": "http://r/11.dem.bz2"},
        })
    assert [r.match_id for r in src.fetch_new(None)] == [11]


def test_cursor_and_limit(monkeypatch):
    rows = [ranked(i) for i in (10, 11, 12, 13, 1000)]
    details = {i: {"patch": 60, "replay_url": f"http://r/{i}.dem.bz2"}
               for i in (10, 11, 12, 13)}
    src = make_source(monkeypatch, rows, details, limit_per_cycle=2)
    assert [r.match_id for r in src.fetch_new("10")] == [11, 12]


def test_latest_patch_autodetect(monkeypatch):
    src = make_source(
        monkeypatch, [ranked(10)],
        details={10: {"patch": 60, "replay_url": "http://r/10.dem.bz2"}},
        patches=[{"id": 59, "name": "7.40"}, {"id": 60, "name": "7.41"}])
    list(src.fetch_new(None))
    assert src._min_patch == 60
