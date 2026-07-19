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
