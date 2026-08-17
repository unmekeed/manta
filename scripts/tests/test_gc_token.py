"""Тесты входа по refresh-токену (scripts/gc_token.py, спринт 137).

Сеть и настоящий Steam здесь не участвуют: проверяется разбор токена и
сборка сообщения входа, то есть ровно та часть, где мы можем ошибиться
сами. Ответит ли на это сообщение Valve — вопрос живого прогона, тестом
он не закрывается и не притворяется закрытым.

Отдельная забота — чтобы токен не утёк. Он равносилен паролю, а путь у
него длиннее пароля: файл на диске, разбор, сообщение, ошибки. В каждом
из этих мест он мог бы оказаться в выводе.
"""
import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gc_token import (EXPIRY_WARN_DAYS, TokenError,  # noqa: E402
                      check_expiry, login_with_token, read_token,
                      token_expiry, token_payload, token_steamid)

STEAMID = 76561198653908422


def _jwt(payload: dict) -> str:
    """JWT с настоящей структурой и фиктивной подписью.

    Подпись здесь произвольная намеренно: модуль её не проверяет, и это
    осознанное решение — подпись Valve, проверять её нам нечем. Тест
    закрепляет именно это поведение.
    """
    def part(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f'{part({"typ": "JWT", "alg": "EdDSA"})}.{part(payload)}.подпись'


def _token(days_left: float = 200, steamid: int = STEAMID) -> str:
    return _jwt({"iss": "steam", "sub": str(steamid),
                 "aud": ["client", "web"],
                 "exp": int(time.time() + days_left * 86400),
                 "iat": int(time.time())})


# -- чтение с диска ---------------------------------------------------------------

def test_missing_token_names_the_fix(tmp_path):
    """Нет файла — ошибка говорит, ЧТО запустить."""
    with pytest.raises(TokenError, match="make gc-token"):
        read_token(tmp_path / "нет-такого")


def test_empty_token_is_not_mistaken_for_a_token(tmp_path):
    """Пустой файл — это отсутствие токена, а не пустой токен.

    Без этой проверки пустая строка уехала бы в сообщение входа, и Steam
    ответил бы InvalidPassword — то есть ровно тем же, чем отвечает на
    отозванный токен. Мы бы снова искали уже известную причину.
    """
    path = tmp_path / "refresh-token"
    path.write_text("   \n")
    with pytest.raises(TokenError, match="пуст"):
        read_token(path)


def test_token_is_read_stripped(tmp_path):
    """Перевод строки от записи файла в токен не попадает.

    Файл пишется с '\\n' в конце — так его кладёт Node. Уехавший в
    сообщение входа лишний символ сделал бы токен недействительным.
    """
    token = _token()
    path = tmp_path / "refresh-token"
    path.write_text(token + "\n", encoding="utf-8")
    assert read_token(path) == token


# -- разбор -----------------------------------------------------------------------

def test_steamid_comes_from_the_token_itself():
    """steamid берётся из токена, а не передаётся отдельно.

    Это то, что позволяет входить, не зная имени аккаунта: при входе по
    токену имя в сообщении не передаётся вовсе.
    """
    assert token_steamid(_token(steamid=STEAMID)) == STEAMID


def test_payload_is_read_without_padding():
    """base64url в JWT идёт без выравнивания — добиваем сами.

    Длина полезной нагрузки почти всегда не кратна четырём, и разбор без
    добивки падает на большинстве настоящих токенов.
    """
    for extra in range(4):
        token = _jwt({"sub": str(STEAMID), "pad": "x" * extra})
        assert token_payload(token)["sub"] == str(STEAMID)


@pytest.mark.parametrize("junk", [
    "совсем не токен",
    "две.части",
    "четыре.части.тут.лишние",
    "заголовок.не-base64-!!!.подпись",
])
def test_broken_tokens_fail_clearly(junk):
    with pytest.raises(TokenError):
        token_payload(junk)


def test_token_without_steamid_is_rejected():
    with pytest.raises(TokenError, match="sub"):
        token_steamid(_jwt({"exp": 1}))


def test_expiry_is_optional_not_fatal():
    """Нет поля exp — работаем, но говорим об этом.

    Формат токена задаёт Valve и может поменять; отказываться работать
    из-за незнакомого поля значило бы ломать замер там, где он ещё
    рабочий.
    """
    assert token_expiry(_jwt({"sub": "1"})) is None
    assert "неизвест" in check_expiry(_jwt({"sub": "1"}))


# -- срок годности ----------------------------------------------------------------

def test_expired_token_fails_before_the_network(tmp_path):
    """Просроченный токен — ошибка ДО попытки входа.

    Steam отвечает на просроченный токен тем же InvalidPassword, что и на
    неверный пароль. Если не проверить срок заранее, диагноз придётся
    восстанавливать заново по неразличимому симптому.
    """
    with pytest.raises(TokenError, match="просрочен"):
        check_expiry(_token(days_left=-1))


def test_soon_expiring_token_warns_but_works():
    msg = check_expiry(_token(days_left=EXPIRY_WARN_DAYS - 1))
    assert "ВНИМАНИЕ" in msg


def test_healthy_token_reports_days_left():
    msg = check_expiry(_token(days_left=100))
    assert "ВНИМАНИЕ" not in msg
    assert "100" in msg


# -- сборка сообщения входа -------------------------------------------------------

class _FakeClient:
    """SteamClient ровно в том объёме, который трогает login_with_token."""

    chat_mode = 2

    def __init__(self, pre_login_ok=True, eresult=1):
        self.sent = []
        self._pre_login_ok = pre_login_ok
        self._eresult = eresult

    def _pre_login(self):
        from steam.enums import EResult
        return EResult.OK if self._pre_login_ok else EResult.TryAnotherCM

    def send(self, message):
        self.sent.append(message)

    def wait_msg(self, emsg, timeout=None):
        if self._eresult is None:
            return None
        return type("Resp", (), {"body": type("B", (), {
            "eresult": self._eresult})()})()


# Библиотека steam нужна ТОЛЬКО тестам сборки сообщения — разбор токена
# от неё не зависит. Пропуск ставится на них поимённо, а не на весь файл:
# `importorskip` на уровне модуля уносил в пропуск и те тесты, которым
# библиотека не нужна, и файл выглядел зелёным, не проверив ничего.
needs_steam = pytest.mark.skipif(
    importlib.util.find_spec("steam") is None,
    reason="steam не установлен (ставится в venv замера: make gc-venv)")


@needs_steam
def test_logon_message_carries_token_and_no_account_name():
    """Три обязательных отличия от обычного входа разом.

    Имя аккаунта и пароль не передаются, токен передаётся, steamid стоит
    в ЗАГОЛОВКЕ — по нему CM и определяет, кто входит. Пустой шаблонный
    steamid, который ставит штатный login(), для входа по токену не
    годится.
    """
    client = _FakeClient()
    token = _token()
    login_with_token(client, token)

    assert len(client.sent) == 1
    msg = client.sent[0]
    assert msg.body.access_token == token
    assert msg.body.account_name == ""
    assert msg.body.password == ""
    assert msg.header.steamid == STEAMID


@needs_steam
def test_login_stops_when_connection_is_not_ready():
    """Не удалось подключиться — сообщение не отправляется вовсе."""
    from steam.enums import EResult
    client = _FakeClient(pre_login_ok=False)
    assert login_with_token(client, _token()) == EResult.TryAnotherCM
    assert client.sent == []


@needs_steam
def test_silence_from_steam_is_a_failure_not_a_success():
    """Ответа нет — это отказ. Иначе замер пошёл бы работать без входа."""
    from steam.enums import EResult
    client = _FakeClient(eresult=None)
    assert login_with_token(client, _token()) == EResult.Fail


def test_token_never_appears_in_error_messages(tmp_path):
    """Токен равносилен паролю и не должен утекать в вывод.

    Проверяются все пути, где он проходит через сообщение об ошибке.
    """
    secret = "СЕКРЕТНАЯ-ЧАСТЬ-ТОКЕНА"
    bad = f"заголовок.{secret}.подпись"
    for call in (lambda: token_payload(bad),
                 lambda: token_steamid(bad),
                 lambda: check_expiry(_token(days_left=-5))):
        with pytest.raises(TokenError) as exc:
            call()
        assert secret not in str(exc.value)
