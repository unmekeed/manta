"""Тесты добычи refresh-токена (scripts/gc-node/get-token.mjs, спринт 137).

Токен равносилен паролю: кто им владеет, тот входит в аккаунт. Поэтому
проверяется не «скрипт отработал», а обращение с секретом — права на
файле, отсутствие токена в выводе — и то, что молчаливый неуспех
невозможен.

steam-user подменяется заглушкой: настоящий Steam потребовал бы учётных
данных и кода Steam Guard, а проверяется здесь наш код, а не Steam.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gc-node" / "get-token.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node не установлен")

# Настоящий по форме JWT: заголовок.нагрузка.подпись, base64url без
# выравнивания. Внутри — steamid и срок годности, как у Valve.
FAKE_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFZERTQSJ9"
    ".eyJzdWIiOiI3NjU2MTE5ODY1MzkwODQyMiIsImV4cCI6NDEwMjQ0NDgwMH0"
    ".fakesignature"
)

STUB = """
const {{ EventEmitter }} = require('events');
class SteamUser extends EventEmitter {{
  logOn() {{
    setTimeout(() => {{
      {body}
    }}, 20);
  }}
  logOff() {{}}
}}
SteamUser.EResult = {{ 5: 'InvalidPassword' }};
module.exports = SteamUser;
"""

TOKEN_THEN_LOGON = ("this.emit('refreshToken', '%s');"
                    "this.emit('loggedOn');" % FAKE_TOKEN)
LOGON_WITHOUT_TOKEN = "this.emit('loggedOn');"
LOGON_ERROR = "this.emit('error', Object.assign(new Error('нет'), "\
              "{eresult: 5}));"


def _run(tmp_path: Path, body: str, creds: bool = True):
    stub_dir = tmp_path / "node_modules" / "steam-user"
    stub_dir.mkdir(parents=True)
    (stub_dir / "index.js").write_text(STUB.format(body=body),
                                       encoding="utf-8")
    (stub_dir / "package.json").write_text(
        json.dumps({"name": "steam-user", "version": "0.0.0",
                    "main": "index.js"}), encoding="utf-8")
    shutil.copy(SCRIPT, tmp_path / "get-token.mjs")
    state = tmp_path / "state"

    env = {k: v for k, v in os.environ.items()
           if k not in ("STEAM_BOT_LOGIN", "STEAM_BOT_PASSWORD")}
    env["GC_STATE_DIR"] = str(state)
    if creds:
        env["STEAM_BOT_LOGIN"] = "mantabotlogin"
        env["STEAM_BOT_PASSWORD"] = "swordfish42"

    proc = subprocess.run(["node", "get-token.mjs"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=25)
    return proc, state / "refresh-token"


def test_token_is_saved(tmp_path):
    proc, token_file = _run(tmp_path, TOKEN_THEN_LOGON)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert token_file.read_text().strip() == FAKE_TOKEN


def test_token_file_is_not_readable_by_others(tmp_path):
    """0600 на файле и 0700 на каталоге.

    Токен — учётные данные. Файл с правами по умолчанию (0644) отдал бы
    доступ к аккаунту любому пользователю машины.
    """
    _, token_file = _run(tmp_path, TOKEN_THEN_LOGON)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700


def test_token_is_never_printed(tmp_path):
    """В вывод идут срок годности и путь, но не сам токен."""
    proc, _ = _run(tmp_path, TOKEN_THEN_LOGON)
    out = proc.stdout + proc.stderr
    assert FAKE_TOKEN not in out
    assert "swordfish42" not in out
    assert "mantabotlogin" not in out
    assert "ma***" in out


def test_expiry_is_reported_from_the_token(tmp_path):
    """Срок годности читается из самого токена и показывается человеку.

    Без него нельзя понять, когда команду придётся повторить, а
    протухший токен даёт при входе тот же InvalidPassword, что и неверный
    пароль.
    """
    proc, _ = _run(tmp_path, TOKEN_THEN_LOGON)
    assert "2100-01-01" in proc.stdout


def test_login_without_a_token_is_a_failure(tmp_path):
    """Вход прошёл, а токена нет — это неуспех, а не успех.

    Единственный смысл команды — получить токен. Если бы она в этом
    случае завершилась нулём, замер молча остался бы без входа, и причину
    искали бы уже в Game Coordinator.
    """
    proc, token_file = _run(tmp_path, LOGON_WITHOUT_TOKEN)
    assert proc.returncode == 1
    assert not token_file.exists()
    assert "токен не приехал" in proc.stderr


def test_steam_refusal_is_reported_by_name(tmp_path):
    proc, token_file = _run(tmp_path, LOGON_ERROR)
    assert proc.returncode == 1
    assert "InvalidPassword" in proc.stderr
    assert not token_file.exists()


def test_missing_credentials_fail_loudly(tmp_path):
    proc, _ = _run(tmp_path, TOKEN_THEN_LOGON, creds=False)
    assert proc.returncode == 2
    assert "STEAM_BOT_LOGIN" in proc.stderr
