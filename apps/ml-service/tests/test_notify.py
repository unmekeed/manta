"""Тесты определения chat_id (боль: уведомления «переехали» в другой чат
после рестарта, потому что авто-выбор берёт последний написавший чат)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training.notify import TelegramNotifier


def _upd(*chats):
    """getUpdates-ответ: chats = [(id, title|None, type), ...]."""
    return {"result": [
        {"message": {"chat": {"id": cid, "title": title, "type": typ}}}
        for cid, title, typ in chats
    ]}


def test_explicit_chat_id_wins(monkeypatch):
    n = TelegramNotifier(token="t", chat_id="42")
    monkeypatch.setattr(n, "_call", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("getUpdates не должен вызываться при явном chat_id")))
    assert n.resolve_chat_id() == "42"


def test_picks_last_chat_and_warns_on_multiple(monkeypatch, caplog):
    n = TelegramNotifier(token="t", chat_id="")
    monkeypatch.setattr(n, "_call", lambda m, **k: _upd(
        (100, "Личный", "private"),
        (-200, "Manta training", "group"),
    ))
    with caplog.at_level("WARNING"):
        assert n.resolve_chat_id() == "-200"
    assert any("закрепите TELEGRAM_CHAT_ID" in r.message for r in caplog.records)
    assert any("Manta training" in r.message for r in caplog.records)


def test_single_chat_no_warning(monkeypatch, caplog):
    n = TelegramNotifier(token="t", chat_id="")
    monkeypatch.setattr(n, "_call", lambda m, **k: _upd(
        (100, "Личный", "private"),
        (100, "Личный", "private"),
    ))
    with caplog.at_level("WARNING"):
        assert n.resolve_chat_id() == "100"
    assert not caplog.records


def test_empty_updates_returns_none_with_hint(monkeypatch, caplog):
    n = TelegramNotifier(token="t", chat_id="")
    monkeypatch.setattr(n, "_call", lambda m, **k: {"result": []})
    with caplog.at_level("WARNING"):
        assert n.resolve_chat_id() is None
    assert any("getUpdates пуст" in r.message for r in caplog.records)
