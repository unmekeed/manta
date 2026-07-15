"""Тесты OpenDotaSource с замоканным HTTP (без сети и rate limit)."""
import bz2
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources import opendota
from collector.sources.opendota import OpenDotaSource


class FakeResp:
    def __init__(self, payload=None, status=200, content=b""):
        self._payload = payload
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def make_source(monkeypatch, pro_matches, details, downloads=None):
    def fake_get(url, **kwargs):
        if url.endswith("/proMatches"):
            return FakeResp(pro_matches)
        for mid, d in details.items():
            if url.endswith(f"/matches/{mid}"):
                return FakeResp(d) if d is not None else FakeResp(status=404)
        for u, body in (downloads or {}).items():
            if url == u:
                return FakeResp(content=body)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(opendota.requests, "get", fake_get)
    monkeypatch.setattr(opendota.time, "sleep", lambda s: None)
    return OpenDotaSource(limit_per_cycle=2)


def test_fetch_new_orders_and_limits(monkeypatch):
    src = make_source(
        monkeypatch,
        pro_matches=[{"match_id": 30}, {"match_id": 10}, {"match_id": 20}],
        details={
            10: {"replay_url": "http://replay1.valve.net/570/10_1.dem.bz2"},
            20: {"replay_url": "http://replay1.valve.net/570/20_2.dem.bz2"},
            30: {"replay_url": "http://replay1.valve.net/570/30_3.dem.bz2"},
        })
    refs = list(src.fetch_new(None))
    # Лимит 2 за цикл, порядок от старых к новым (курсор монотонный).
    assert [r.match_id for r in refs] == [10, 20]
    assert refs[0].source_cursor == "10"


def test_fetch_new_respects_cursor(monkeypatch):
    src = make_source(
        monkeypatch,
        pro_matches=[{"match_id": 10}, {"match_id": 20}],
        details={20: {"replay_url": "http://r/570/20_2.dem.bz2"}})
    refs = list(src.fetch_new("10"))
    assert [r.match_id for r in refs] == [20]


def test_fetch_new_stops_on_missing_replay(monkeypatch):
    """Реплей без salt останавливает цикл — курсор не перепрыгивает матч."""
    src = make_source(
        monkeypatch,
        pro_matches=[{"match_id": 10}, {"match_id": 20}],
        details={10: {"replay_url": None}, 20: {"replay_url": "http://r/x.dem.bz2"}})
    assert list(src.fetch_new(None)) == []


def test_download_decompresses_and_validates(monkeypatch):
    dem = b"PBDEMS2" + b"\x00" * 64
    url = "http://replay1.valve.net/570/10_1.dem.bz2"
    src = make_source(monkeypatch, pro_matches=[], details={},
                      downloads={url: bz2.compress(dem)})
    ref_cls = opendota.MatchRef
    data = src.download_replay(ref_cls(10, url, "Professional", "10"))
    assert data == dem


def test_download_rejects_garbage(monkeypatch):
    url = "http://replay1.valve.net/570/11_1.dem.bz2"
    src = make_source(monkeypatch, pro_matches=[], details={},
                      downloads={url: bz2.compress(b"<html>not a demo</html>")})
    with pytest.raises(ValueError, match="not a Source 2 demo"):
        src.download_replay(opendota.MatchRef(11, url, "Professional", "11"))
