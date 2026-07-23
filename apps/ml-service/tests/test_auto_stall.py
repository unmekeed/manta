"""Тесты детектора стагнации витрины (runbook «витрина не растёт»)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from training import auto


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return True


def _reset(monkeypatch, notifier):
    monkeypatch.setattr(auto, "_last_growth", (None, 0.0))
    monkeypatch.setattr(auto, "_stall_alerted", False)
    monkeypatch.setattr(auto, "_notifier", notifier)


def test_stall_alert_fires_once_and_rearms(monkeypatch):
    n = FakeNotifier()
    _reset(monkeypatch, n)
    monkeypatch.setenv("DATASET_STALL_ALERT_H", "12")
    clock = [1000.0]
    monkeypatch.setattr(auto.time, "time", lambda: clock[0])

    auto._check_stall(100)             # первая точка — рост
    clock[0] += 6 * 3600
    auto._check_stall(100)             # 6ч без роста — рано
    assert n.sent == []
    clock[0] += 7 * 3600
    auto._check_stall(100)             # 13ч — алерт
    assert len(n.sent) == 1 and "не растёт" in n.sent[0]
    clock[0] += 6 * 3600
    auto._check_stall(100)             # всё ещё стоит — не спамим
    assert len(n.sent) == 1
    auto._check_stall(140)             # рост возобновился
    assert len(n.sent) == 2 and "снова растёт" in n.sent[1]
    # Новый эпизод стагнации снова алертит.
    clock[0] += 13 * 3600
    auto._check_stall(140)
    assert len(n.sent) == 3


def test_growth_never_alerts(monkeypatch):
    n = FakeNotifier()
    _reset(monkeypatch, n)
    clock = [0.0]
    monkeypatch.setattr(auto.time, "time", lambda: clock[0])
    for i in range(5):
        clock[0] += 24 * 3600
        auto._check_stall(100 + i)     # растёт каждый вызов
    assert n.sent == []


def _reset_replay(monkeypatch, notifier):
    monkeypatch.setattr(auto, "_replay_alerted", False)
    monkeypatch.setattr(auto, "_notifier", notifier)


def test_replay_stall_alert_fires_once_and_recovers(monkeypatch):
    """Инцидент №6: витрина растёт по JSON-пути, а ReplayEvents стоит."""
    n = FakeNotifier()
    _reset_replay(monkeypatch, n)
    monkeypatch.setenv("REPLAY_STALL_ALERT_H", "6")
    now = 1_000_000.0
    monkeypatch.setattr(auto.time, "time", lambda: now)
    freshness = [now - 3600]           # вставка час назад — свежо
    monkeypatch.setattr(auto, "_replay_freshness_ts", lambda: freshness[0])

    auto._check_replay_stall()
    assert n.sent == []
    freshness[0] = now - 7 * 3600      # 7ч — протухло
    auto._check_replay_stall()
    assert len(n.sent) == 1 and "реплейный путь стоит" in n.sent[0]
    auto._check_replay_stall()         # всё ещё стоит — не спамим
    assert len(n.sent) == 1
    freshness[0] = now - 60            # парсер снова пишет
    auto._check_replay_stall()
    assert len(n.sent) == 2 and "снова пишет" in n.sent[1]


def test_replay_stall_empty_table_alerts(monkeypatch):
    """ReplayEvents пуста (max(ingested_at)=1970) — путь никогда не писал."""
    n = FakeNotifier()
    _reset_replay(monkeypatch, n)
    monkeypatch.setattr(auto, "_replay_freshness_ts", lambda: 0.0)
    auto._check_replay_stall()
    assert len(n.sent) == 1 and "пуста" in n.sent[0]


def test_replay_stall_silent_when_clickhouse_down(monkeypatch):
    """Недоступный ClickHouse — не повод для алерта реплейного пути."""
    n = FakeNotifier()
    _reset_replay(monkeypatch, n)
    monkeypatch.setattr(auto, "_replay_freshness_ts", lambda: None)
    auto._check_replay_stall()
    assert n.sent == []
