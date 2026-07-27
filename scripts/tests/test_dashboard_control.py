"""Тесты панели управления дашборда (спринт 74).

Панель умеет запускать процессы, поэтому проверяется в первую очередь не
«кнопка работает», а что её нельзя нажать снаружи. Три меры защиты, три
группы тестов: белый список действий, заголовок против CSRF и запрет
действий, когда сервер слушает не loopback.

Тесты поднимают настоящий HTTP-сервер на случайном порту: проверять
обработчик вызовом метода напрямую бессмысленно — половина защиты живёт
именно в разборе запроса.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(base, path, headers=None):
    req = urllib.request.Request(base + path, method="POST",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_post_without_header_is_rejected(server, monkeypatch):
    """Главная проверка CSRF: без нестандартного заголовка запрос не
    выполняется. Иначе любая открытая владельцем страница могла бы
    отправить форму на localhost:9107 и остановить сбор."""
    monkeypatch.setenv("DASHBOARD_BIND", "127.0.0.1")
    code, body = _post(server, "/api/action/doctor")
    assert code == 403
    assert dashboard.ACTION_HEADER in body["error"]


def test_unknown_action_is_rejected(server, monkeypatch):
    """Белый список: имя действия — ключ из ACTIONS, а не команда."""
    monkeypatch.setenv("DASHBOARD_BIND", "127.0.0.1")
    code, body = _post(server, "/api/action/rm-rf",
                       {dashboard.ACTION_HEADER: "1"})
    assert code == 409
    assert not body["ok"]


def test_actions_disabled_when_not_loopback(server, monkeypatch):
    """Панель, видимая из сети, теряет право запускать что-либо —
    пока владелец не разрешит это явно."""
    monkeypatch.setenv("DASHBOARD_BIND", "0.0.0.0")
    monkeypatch.delenv("DASHBOARD_ALLOW_REMOTE_ACTIONS", raising=False)
    assert not dashboard.actions_enabled()
    code, body = _post(server, "/api/action/doctor",
                       {dashboard.ACTION_HEADER: "1"})
    assert code == 403
    assert "loopback" in body["error"]


def test_remote_actions_require_explicit_optin(monkeypatch):
    monkeypatch.setenv("DASHBOARD_BIND", "0.0.0.0")
    monkeypatch.setenv("DASHBOARD_ALLOW_REMOTE_ACTIONS", "1")
    assert dashboard.actions_enabled()


def test_loopback_forms_recognised(monkeypatch):
    for host in ("127.0.0.1", "localhost", "::1"):
        monkeypatch.setenv("DASHBOARD_BIND", host)
        assert dashboard.actions_enabled(), host


def test_job_endpoint_reports_state(server):
    code, body = _get(server, "/api/job")
    assert code == 200
    assert "actions_enabled" in body and "job" in body


def test_action_runs_and_captures_output(server, monkeypatch, tmp_path):
    """Сквозная проверка: действие действительно исполняется, вывод
    попадает в лог, код возврата фиксируется."""
    monkeypatch.setenv("DASHBOARD_BIND", "127.0.0.1")
    monkeypatch.setitem(dashboard.ACTIONS, "selftest", {
        "label": "тест", "argv": ["sh", "-c", "echo привет; exit 3"],
        "danger": False, "hint": ""})
    code, body = _post(server, "/api/action/selftest",
                       {dashboard.ACTION_HEADER: "1"})
    assert code == 200 and body["ok"], body

    for _ in range(50):
        snap = dashboard.JOB.snapshot()
        if not snap["running"] and snap["code"] is not None:
            break
        time.sleep(0.1)
    snap = dashboard.JOB.snapshot()
    assert snap["code"] == 3, snap
    assert any("привет" in line for line in snap["lines"]), snap["lines"]


def test_second_action_rejected_while_running(server, monkeypatch):
    """Слот один: одновременные recover и stop подрались бы за одни и те
    же процессы."""
    monkeypatch.setenv("DASHBOARD_BIND", "127.0.0.1")
    monkeypatch.setitem(dashboard.ACTIONS, "slow", {
        "label": "долгое", "argv": ["sleep", "5"], "danger": False, "hint": ""})
    code, body = _post(server, "/api/action/slow",
                       {dashboard.ACTION_HEADER: "1"})
    assert code == 200 and body["ok"]
    code2, body2 = _post(server, "/api/action/slow",
                         {dashboard.ACTION_HEADER: "1"})
    assert code2 == 409 and not body2["ok"]
    assert "уже выполняется" in body2["message"]
    dashboard.JOB._proc.kill()


def test_every_action_is_argv_not_shell():
    """argv-список, а не строка: строка ушла бы в оболочку, и любая
    правка конфигурации превратилась бы в инъекцию."""
    for name, spec in dashboard.ACTIONS.items():
        assert isinstance(spec["argv"], list), name
        assert all(isinstance(a, str) for a in spec["argv"]), name


# -- Виды обучения (спринт 75) -------------------------------------------------

def test_training_endpoint_degrades_gracefully(server, monkeypatch, tmp_path):
    """Когда auto-train и ClickHouse не подняты, раздел обучения обязан
    вернуть пустые поля, а не упасть: дашборд — то, что смотрят ИМЕННО
    когда всё лежит."""
    monkeypatch.setattr(dashboard, "ABLATION_JSON", tmp_path / "нет.json")
    monkeypatch.setattr(dashboard, "_growth_cache", {"ts": 0.0, "rows": []})
    code, body = _get(server, "/api/training")
    assert code == 200
    assert body["autotrain_up"] is False
    assert body["phases"] == {"early": None, "mid": None, "late": None}
    assert body["ablation"]["rows"] == []


def test_ablation_report_is_read_when_present(monkeypatch, tmp_path):
    path = tmp_path / "ablation.json"
    path.write_text(json.dumps({
        "base_brier": 0.1484,
        "rows": [{"target": "F3_драфт_прайор", "coverage": 1.0,
                  "delta": 0.00034, "verdict": "эффект ничтожен"}],
    }), encoding="utf-8")
    monkeypatch.setattr(dashboard, "ABLATION_JSON", path)
    a = dashboard._ablation()
    assert a["base_brier"] == 0.1484
    assert a["rows"][0]["target"] == "F3_драфт_прайор"


def test_psi_top_sorted_desc(monkeypatch):
    """PSI показываем худшими вперёд: именно большие значения означают,
    что модель обучена не на том, что сейчас приходит."""
    fake = {("wp_feature_psi", (("feature", "a"),)): 0.05,
            ("wp_feature_psi", (("feature", "b"),)): 0.9,
            ("wp_feature_psi", (("feature", "c"),)): 0.4,
            ("wp_psi_max", ()): 0.9}
    monkeypatch.setattr(dashboard, "_scrape", lambda port: fake)
    monkeypatch.setattr(dashboard, "_clickhouse_growth", lambda: [])
    t = dashboard.training_snapshot()
    assert [r["feature"] for r in t["psi_top"]] == ["b", "c", "a"]
    assert t["psi_max"] == 0.9


def test_ablation_action_writes_json():
    """Кнопка обязана запускать ablation с --json: иначе результат остался
    бы только в консоли задания, и таблице нечего показывать."""
    argv = dashboard.ACTIONS["ablation"]["argv"]
    assert any("--json" in a for a in argv), argv
