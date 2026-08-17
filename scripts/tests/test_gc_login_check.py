"""Тесты проверки входа в Steam новым путём (scripts/gc-node/login-check.mjs).

Проверяется одно свойство, и оно важнее остальных: скрипт ОБЯЗАН
завершиться сам. Первая версия печатала вердикт только внутри обработчика
`accountLimitations`, а это событие приходит не всегда — на живом
аккаунте оно не пришло вовсе. Вход при этом прошёл успешно: ответ на
единственный вопрос, ради которого скрипт написан, был получен и не
показан, а процесс висел, пока его не сняли с клавиатуры.

Настоящий Steam здесь не нужен и не годится: он требует учётных данных,
кода Steam Guard и сети, а проверяем мы поведение НАШЕГО кода на
сообщениях Steam, а не сам Steam. Поэтому steam-user подменяется
заглушкой, которая эмитит нужные события — в том числе тот случай, когда
необязательное событие не приходит.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gc-node" / "login-check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node не установлен")

# Заглушка steam-user. logOn() эмитит loggedOn, а дальше — по сценарию.
STUB = """
const {{ EventEmitter }} = require('events');
class SteamUser extends EventEmitter {{
  logOn() {{
    setTimeout(() => {{
      this.steamID = {{ getSteamID64: () => '7656119TEST' }};
      {on_logon}
    }}, 20);
  }}
  logOff() {{}}
}}
SteamUser.EResult = {{ 5: 'InvalidPassword', 84: 'RateLimitExceeded' }};
module.exports = SteamUser;
"""

LOGGED_ON = "this.emit('loggedOn');"
WITH_LIMITS = (LOGGED_ON +
               "setTimeout(() => this.emit('accountLimitations', "
               "%s, false, false), 20);")
LOGON_ERROR = "this.emit('error', Object.assign(new Error('нет'), "\
              "{eresult: %d}));"


# Заведомо непохожие на текст скрипта строки: сам скрипт упоминает
# «gc-probe.py», и логин вроде «probe» сделал бы проверку «секрет не
# попал в вывод» ложно-срабатывающей на собственных подсказках.
FAKE_LOGIN = "mantabotlogin"
FAKE_PASSWORD = "swordfish42"


def _run(tmp_path: Path, on_logon: str, timeout: int = 25,
         creds: bool = True):
    stub_dir = tmp_path / "node_modules" / "steam-user"
    stub_dir.mkdir(parents=True)
    (stub_dir / "index.js").write_text(STUB.format(on_logon=on_logon),
                                       encoding="utf-8")
    (stub_dir / "package.json").write_text(
        json.dumps({"name": "steam-user", "version": "0.0.0",
                    "main": "index.js"}), encoding="utf-8")
    shutil.copy(SCRIPT, tmp_path / "login-check.mjs")

    env = {k: v for k, v in os.environ.items()
           if k not in ("STEAM_BOT_LOGIN", "STEAM_BOT_PASSWORD")}
    if creds:
        env["STEAM_BOT_LOGIN"] = FAKE_LOGIN
        env["STEAM_BOT_PASSWORD"] = FAKE_PASSWORD

    started = time.monotonic()
    proc = subprocess.run(
        ["node", "login-check.mjs"], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=timeout)
    return proc, time.monotonic() - started


def test_exits_when_limitations_never_arrive(tmp_path):
    """Событие с ограничениями не пришло — вердикт всё равно напечатан.

    Ровно тот случай, на котором скрипт висел у пользователя. `timeout`
    у subprocess здесь и есть проверка: зависший процесс уронит тест
    исключением, а не молчаливым успехом.
    """
    proc, _ = _run(tmp_path, LOGGED_ON)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ВЕРДИКТ" in proc.stdout
    assert "Новый путь аутентификации РАБОТАЕТ" in proc.stdout
    assert "ограничения Steam не прислал" in proc.stdout


def test_does_not_wait_when_limitations_arrive(tmp_path):
    """Ограничения пришли — вердикт сразу, без ожидания таймера.

    Без этой проверки «не висит» можно было бы обеспечить одним таймером,
    и скрипт всегда ждал бы отведённые секунды впустую.
    """
    proc, elapsed = _run(tmp_path, WITH_LIMITS % "true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "limited (нет $5 пополнения): да" in proc.stdout
    assert "аккаунт limited" in proc.stdout
    assert elapsed < 3, f"ждал {elapsed:.1f} с, хотя событие пришло сразу"


def test_verdict_is_printed_once_when_limitations_arrive_early(tmp_path):
    """Событие обогнало таймер — вердикт один."""
    proc, _ = _run(tmp_path, WITH_LIMITS % "false")
    assert proc.stdout.count("=== ВЕРДИКТ ===") == 1


def test_verdict_is_printed_once_when_limitations_arrive_late(tmp_path):
    """Событие пришло ПОСЛЕ таймера — вердикт всё равно один.

    Этот случай и есть настоящая проверка однократности, а «событие
    раньше таймера» — нет. Печать стерегут два независимых механизма:
    снятие таймера и выход из процесса. Пока событие приходит первым,
    достаточно любого одного, и снятие второго тест не заметит. Здесь
    первым срабатывает таймер, и если выход из finish убрать, опоздавшее
    событие допечатает второй вердикт.

    Медленный (ждём дольше пяти секунд таймера) и потому единственный
    такой. Ускорять его, сделав задержку настраиваемой из окружения, я не
    стал: ровно этим приёмом тесты якоря WSL уже один раз проверили
    конфигурацию, в которой система не работает, и пропустили дефект.
    """
    late = LOGGED_ON + ("setTimeout(() => this.emit('accountLimitations', "
                        "false, false, false), 17000);")
    proc, elapsed = _run(tmp_path, late, timeout=40)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("=== ВЕРДИКТ ===") == 1, proc.stdout
    assert elapsed < 17, "вышли по событию, а не по таймеру — проверка мимо"


def test_invalid_password_is_reported_as_not_the_protocol(tmp_path):
    """Отказ по паролю НОВЫМ путём снимает гипотезу про протокол.

    Это второй возможный исход проверки, и перепутать его с первым
    нельзя: он означает, что переписывать замер незачем.
    """
    proc, _ = _run(tmp_path, LOGON_ERROR % 5)
    assert proc.returncode == 1
    assert "InvalidPassword" in proc.stderr
    assert "не в протоколе" in proc.stderr


def test_rate_limit_is_named_not_confused_with_a_wrong_password(tmp_path):
    """84 — это пауза Steam за частые попытки, а не неверный пароль."""
    proc, _ = _run(tmp_path, LOGON_ERROR % 84)
    assert proc.returncode == 1
    assert "RateLimitExceeded" in proc.stderr
    assert "паузу" in proc.stderr


def test_missing_credentials_fail_loudly(tmp_path):
    """Без секретов — внятный отказ и код 2, а не попытка входа."""
    proc, _ = _run(tmp_path, LOGGED_ON, creds=False)
    assert proc.returncode == 2
    assert "STEAM_BOT_LOGIN" in proc.stderr


def test_password_is_never_printed(tmp_path):
    """Пароль не попадает в вывод ни одним из путей.

    Логин печатается двумя первыми символами — то же правило, что в
    gc-probe.py. Длина пароля печатается намеренно: без неё не отличить
    «пароль неверен» от «шелл съел спецсимвол при чтении env-файла».
    """
    for n, scenario in enumerate((LOGGED_ON, WITH_LIMITS % "true",
                                  LOGON_ERROR % 5)):
        proc, _ = _run(tmp_path / f"s{n}", scenario)
        out = proc.stdout + proc.stderr
        assert FAKE_PASSWORD not in out, out
        assert FAKE_LOGIN not in out, out
        assert FAKE_LOGIN[:2] + "***" in out
