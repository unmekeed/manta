"""Тесты OpenDotaSource с замоканным HTTP (без сети и rate limit)."""
import bz2
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from collector.sources import opendota
from collector.sources.opendota import OpenDotaSource


class FakeResp:
    def __init__(self, payload=None, status=200, content=b"", headers=None):
        self._payload = payload
        self.status_code = status
        self.content = content
        # Content-Length нужен, чтобы отличить обрыв от порчи файла.
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def make_source(monkeypatch, pro_matches, details, downloads=None,
                headers=None):
    def fake_get(url, **kwargs):
        if url.endswith("/proMatches"):
            return FakeResp(pro_matches)
        for mid, d in details.items():
            if url.endswith(f"/matches/{mid}"):
                return FakeResp(d) if d is not None else FakeResp(status=404)
        for u, body in (downloads or {}).items():
            if url == u:
                return FakeResp(content=body,
                                headers=(headers or {}).get(u))
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
    # Постоянная ошибка, а не ValueError: коллектор обязан отличить её от
    # временного сбоя и сдвинуть курсор через этот матч.
    with pytest.raises(opendota.PermanentDownloadError,
                       match="not a Source 2 demo"):
        src.download_replay(opendota.MatchRef(11, url, "Professional", "11"))


def test_download_of_corrupt_bz2_is_permanent(monkeypatch):
    """Битый bz2 (то, на чём встал реплейный путь) — постоянная ошибка."""
    url = "http://replay1.valve.net/570/12_1.dem.bz2"
    src = make_source(monkeypatch, pro_matches=[], details={},
                      downloads={url: b"BZh9 truncated garbage"})
    with pytest.raises(opendota.PermanentDownloadError, match="битый bz2"):
        src.download_replay(opendota.MatchRef(12, url, "Professional", "12"))


def test_truncated_download_is_retryable_not_permanent(monkeypatch):
    """Обрыв на большом файле выглядит как битый bz2, но это НЕ порча:
    инцидент 2026-07-31 — каждый обрыв выбрасывал исправный матч
    навсегда, и реплейный путь стоял с processed=0."""
    url = "http://replay1.valve.net/570/13_1.dem.bz2"
    full = bz2.compress(b"PBDEMS2" + b"\0" * 4096)
    src = make_source(monkeypatch, pro_matches=[], details={},
                      downloads={url: full[: len(full) // 2]},
                      headers={url: {"Content-Length": str(len(full))}})
    with pytest.raises(opendota.IncompleteDownloadError, match="обрыв"):
        src.download_replay(opendota.MatchRef(13, url, "Professional", "13"))


def test_non_bz2_body_is_permanent(monkeypatch):
    """Заглушка или HTML с кодом 200 — не «битый архив», а мусор:
    повторять бессмысленно."""
    url = "http://replay1.valve.net/570/14_1.dem.bz2"
    src = make_source(monkeypatch, pro_matches=[], details={},
                      downloads={url: b"<html>503 Service Unavailable</html>"})
    with pytest.raises(opendota.PermanentDownloadError, match="не bz2"):
        src.download_replay(opendota.MatchRef(14, url, "Professional", "14"))


def test_complete_download_still_works(monkeypatch):
    """Целый файл с корректным Content-Length проходит как раньше."""
    dem = b"PBDEMS2" + b"\0" * 2048
    url = "http://replay1.valve.net/570/15_1.dem.bz2"
    blob = bz2.compress(dem)
    src = make_source(monkeypatch, pro_matches=[], details={},
                      downloads={url: blob},
                      headers={url: {"Content-Length": str(len(blob))}})
    assert src.download_replay(
        opendota.MatchRef(15, url, "Professional", "15")) == dem
