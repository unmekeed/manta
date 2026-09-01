"""Вход по сохранённому токену через Node (спринт 166).

ЗАЧЕМ ЭТОТ СКРИПТ ЕСТЬ. Питоновский замер до Steam доходит, но ни один
сервер входа его не принимает: пять разных CM подряд отвечают
TryAnotherCM. `steam==1.4.4` — последняя версия на PyPI, шлёт ClientLogon
с версией клиента 2019 года. Похоже, Valve этот путь больше не
принимает — но «похоже» не «знаем», а решение переписывать замер на Node
слишком дорогое, чтобы принимать его по догадке.

Скрипт и есть недостающее измерение: тот же аккаунт, тот же токен, та же
машина, другая библиотека.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. Три свойства, и все три уже стоили в этом проекте
времени:

  * пароль не нужен — вход идёт токеном. Со спринта 157 пароль с машины
    стёрт намеренно, и проверка, требующая его вернуть, ослабляла бы
    защиту ради диагностики. Существующий login-check.mjs требует именно
    пароль, поэтому здесь отдельный скрипт, а не флаг к нему;
  * скрипт ЗАВЕРШАЕТСЯ сам в любом исходе. На этом уже обжигались: первая
    версия login-check.mjs печатала вердикт внутри необязательного
    события и висела, когда оно не приходило;
  * секрет не печатается.

Настоящий Steam не нужен и не годится: проверяется поведение НАШЕГО кода
на сообщениях Steam, поэтому steam-user подменяется заглушкой.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gc-node" / "token-login.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node не установлен")

STUB = """
const {{ EventEmitter }} = require('events');
class SteamUser extends EventEmitter {{
  logOn(details) {{
    global.__logOnArgs = details;
    setTimeout(() => {{
      this.steamID = {{ getSteamID64: () => '76561199TEST' }};
      {on_logon}
    }}, 20);
  }}
  logOff() {{}}
}}
SteamUser.EResult = {{ 5: 'InvalidPassword', 15: 'AccessDenied',
                     AccessDenied: 15, InvalidPassword: 5 }};
module.exports = SteamUser;
"""

LOGGED_ON = "this.emit('loggedOn', {cell_id: 42});"
LOGON_ERROR = ("this.emit('error', Object.assign(new Error('нет'), "
               "{eresult: %d}));")
SILENT = "/* Steam молчит */"

# Настоящий JWT не нужен: читается только полезная нагрузка. Срок —
# заведомо в будущем, чтобы «просрочен» не путалось с «не пускают».
FUTURE = 4102444800          # 2100-01-01
TOKEN = ("хедер." +
         __import__("base64").urlsafe_b64encode(
             json.dumps({"exp": FUTURE}).encode()).decode().rstrip("=") +
         ".подпись")


def _run(tmp_path: Path, on_logon: str, token: str | None = TOKEN,
         timeout: int = 25, wait_ms: int = 3000):
    stub_dir = tmp_path / "node_modules" / "steam-user"
    stub_dir.mkdir(parents=True)
    (stub_dir / "index.js").write_text(STUB.format(on_logon=on_logon),
                                       encoding="utf-8")
    (stub_dir / "package.json").write_text(
        json.dumps({"name": "steam-user", "version": "0.0.0",
                    "main": "index.js"}), encoding="utf-8")
    shutil.copy(SCRIPT, tmp_path / "token-login.mjs")

    state = tmp_path / "state"
    state.mkdir()
    if token is not None:
        (state / "refresh-token").write_text(token + "\n", encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if k not in ("STEAM_BOT_LOGIN", "STEAM_BOT_PASSWORD")}
    env["GC_STATE_DIR"] = str(state)
    # Штатное ожидание — минута; в тесте столько ждать незачем, а
    # выкручивать таймаут теста вместо таймаута скрипта значило бы
    # проверять терпение pytest, а не поведение кода.
    env["GC_LOGIN_TIMEOUT_MS"] = str(wait_ms)

    started = time.monotonic()
    proc = subprocess.run(["node", "token-login.mjs"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=timeout)
    return proc, time.monotonic() - started


def test_successful_login_is_reported_and_exits(tmp_path):
    """Главный исход: вошли — сказали и вышли.

    Именно этот ответ и решает судьбу ветки GC: если Node входит там, где
    Python получает TryAnotherCM, дело в библиотеке, а не в аккаунте.
    """
    proc, _ = _run(tmp_path, LOGGED_ON)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "NODE ВОШЁЛ" in proc.stdout
    assert "76561199TEST" in proc.stdout


def test_password_is_never_needed(tmp_path):
    """Вход идёт ТОКЕНОМ, а не логином с паролем.

    Со спринта 157 пароль с машины стёрт намеренно. Проверка, требующая
    вернуть его на публичную машину, стоила бы дороже, чем стоит ответ,
    который она даёт.
    """
    # Печать ДО события: обработчик loggedOn завершает процесс, и всё,
    # что стоит после него, не выполнится никогда.
    proc, _ = _run(tmp_path, "console.log('ARGS:' + "
                   "JSON.stringify(global.__logOnArgs));" + LOGGED_ON)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    args = json.loads(proc.stdout.split("ARGS:")[1].splitlines()[0])
    assert "refreshToken" in args
    assert "password" not in args and "accountName" not in args


def test_token_is_not_printed(tmp_path):
    """Токен равносилен паролю и в вывод попадать не должен."""
    proc, _ = _run(tmp_path, LOGGED_ON)
    assert TOKEN not in proc.stdout + proc.stderr
    assert TOKEN.split(".")[1] not in proc.stdout + proc.stderr


def test_refusal_is_reported_and_exits(tmp_path):
    """Отказ — тоже ответ, и он обязан быть назван.

    Молчаливый выход с ошибкой оставил бы нас ровно там же, где мы были с
    питоновским замером: «что-то не так, но что — неизвестно».
    """
    proc, _ = _run(tmp_path, LOGON_ERROR % 15)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "AccessDenied" in out
    assert "make gc-token" in out, "отказ по токену не подсказывает лечение"
    # Мало назвать команду: «AccessDenied» при входе ТОКЕНОМ читается как
    # «аккаунту запрещено», хотя означает «токен недействителен». Без
    # этого пояснения подсказка выглядит необоснованной, и первым делом
    # начинают проверять аккаунт. Мутация, убравшая ровно эту строку,
    # пережила первый заход проверки.
    assert "отозван" in out


def test_silence_ends_by_itself(tmp_path):
    """Steam не ответил — скрипт всё равно завершается.

    Ровно на этом висел login-check.mjs: вердикт печатался внутри
    необязательного события. Здесь молчание проверяется коротким
    таймаутом самого скрипта.
    """
    proc, spent = _run(tmp_path, SILENT, timeout=20, wait_ms=1500)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "не ответил" in proc.stdout + proc.stderr
    assert spent < 15, f"скрипт ждал {spent:.0f}с вместо своего таймаута"


def test_missing_token_says_what_to_do(tmp_path):
    """Нет токена — внятный отказ, а не трассировка.

    Отличать «токена нет» от «Valve не пускает» обязательно: лечатся они
    в разных местах, и спутать их значит искать беду в Steam.
    """
    proc, _ = _run(tmp_path, LOGGED_ON, token=None)
    assert proc.returncode == 2
    assert "make gc-token" in proc.stdout + proc.stderr


def test_empty_token_is_not_sent_to_steam(tmp_path):
    """Пустой файл — тоже «токена нет», а не повод сходить в Steam.

    Пустая строка ушла бы в logOn и вернулась отказом, неотличимым от
    протухшего токена.
    """
    proc, _ = _run(tmp_path, LOGGED_ON, token="")
    assert proc.returncode == 2
    assert "пуст" in proc.stdout + proc.stderr


def test_state_dir_matches_the_token_writer():
    """Читаем оттуда же, куда пишет get-token.mjs.

    Разъедься эти пути — скрипт сообщал бы «токена нет» там, где токен
    есть, и мы искали бы беду в Steam. Соседний login-check.mjs
    запускается с ДРУГИМ каталогом (~/.manta-gc-node), так что разойтись
    здесь легко.
    """
    writer = (SCRIPT.parent / "get-token.mjs").read_text(encoding="utf-8")
    reader = SCRIPT.read_text(encoding="utf-8")
    for src in (writer, reader):
        assert "GC_STATE_DIR" in src and "'.manta-gc'" in src, src[:200]
