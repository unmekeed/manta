"""Вход в Steam по refresh-токену (гибридный путь, спринт 137).

ЗАЧЕМ. Библиотека steam==1.4.4 логинится устаревшим способом — пароль
открытым полем в EMsg.ClientLogon, — а Valve этот путь отключила: он
отвечает InvalidPassword при любом верном пароле. Нового механизма
(CAuthentication) в библиотеке нет и не появится: 1.4.4 — последняя
версия на PyPI, в master та же строка.

Но в CMsgClientLogon есть поле `access_token`, и вход ИМ библиотека
отправить умеет — просто её метод login() такого аргумента не
принимает. Значит недостающего ровно два: сам токен и правильно
собранное сообщение. Токен добывает Node (scripts/gc-node/get-token.mjs,
пароль в обмен на токен через новый механизм), сообщение собирается
здесь. Всё остальное — работа с Game Coordinator, режимы замера,
проверка соли — остаётся питоновским и нетронутым.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ login(). Тремя вещами, и каждая обязательна:

  * `access_token` вместо `password`;
  * `account_name` НЕ ставится вовсе — при входе по токену имя не
    передаётся, аккаунт определяется самим токеном;
  * steamid в ЗАГОЛОВКЕ сообщения — настоящий, а не пустой шаблон
    Individual/Public, который отправляет login(). Токен это JWT, и его
    полезная нагрузка несёт steamid в поле `sub`, поэтому передавать его
    отдельно не нужно.

БЕЗОПАСНОСТЬ. Токен равносилен паролю: он не печатается, не логируется и
не попадает в сообщения об ошибках. Подпись JWT здесь НЕ проверяется, и
это не упущение — подпись Valve, проверять её нам нечем и незачем.
Полезная нагрузка читается только чтобы узнать steamid и срок годности;
доверять ей сверх этого нельзя, и мы не доверяем: если токен подделан,
Steam откажет во входе.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

STATE_DIR = Path(os.getenv("GC_STATE_DIR",
                           Path.home() / ".manta-gc")).expanduser()
TOKEN_PATH = STATE_DIR / "refresh-token"

# За сколько до истечения предупреждать. Токен живёт месяцами, и молча
# протухший токен посреди замера выглядел бы как отказ Game Coordinator.
EXPIRY_WARN_DAYS = 7


class TokenError(RuntimeError):
    """Токена нет, он испорчен или просрочен."""


def read_token(path: Path | None = None) -> str:
    """Прочитать токен с диска. Внятная ошибка вместо пустой строки."""
    path = path or TOKEN_PATH
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise TokenError(
            f"нет токена ({path}). Получить: make gc-token") from None
    except OSError as exc:
        raise TokenError(f"токен не читается ({path}): {exc}") from None
    if not token:
        raise TokenError(f"файл токена пуст ({path}). Получить: make gc-token")
    return token


def token_payload(token: str) -> dict:
    """Полезная нагрузка JWT. Подпись не проверяется (см. модуль)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("токен не похож на JWT (ожидались три части)")
    raw = parts[1]
    # base64url без выравнивания — добиваем '=' сами.
    raw += "=" * (-len(raw) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(raw))
    except Exception:
        raise TokenError("полезная нагрузка токена не разбирается") from None


def token_steamid(token: str) -> int:
    """SteamID владельца токена — поле `sub`."""
    sub = token_payload(token).get("sub")
    try:
        return int(sub)
    except (TypeError, ValueError):
        raise TokenError("в токене нет steamid (поле sub)") from None


def token_expiry(token: str) -> int | None:
    """Момент истечения, unix-время. None, если поля нет."""
    exp = token_payload(token).get("exp")
    try:
        return int(exp)
    except (TypeError, ValueError):
        return None


def check_expiry(token: str, now: float | None = None) -> str:
    """Строка о сроке годности. Просроченный токен — сразу ошибка.

    Просроченный токен даёт при входе тот же InvalidPassword, что и
    неверный пароль, и без этой проверки мы бы заново искали причину,
    которая уже известна.
    """
    now = time.time() if now is None else now
    exp = token_expiry(token)
    if exp is None:
        return "срок годности неизвестен"
    left_days = (exp - now) / 86400
    if left_days <= 0:
        raise TokenError(
            f"токен просрочен {-left_days:.0f} дн. назад. "
            "Обновить: make gc-token")
    if left_days <= EXPIRY_WARN_DAYS:
        return f"ВНИМАНИЕ: токен истекает через {left_days:.1f} дн."
    return f"токен годен ещё {left_days:.0f} дн."


def login_with_token(client, token: str, timeout: int = 30):
    """Войти существующим SteamClient по refresh-токену.

    Возвращает EResult. Импорты внутри: модуль читается и тестируется без
    установленной библиотеки steam, а она живёт в отдельном venv.
    """
    from steam.core.msg import MsgProto
    from steam.enums import EOSType, EResult
    from steam.enums.emsg import EMsg
    from steam.steamid import SteamID

    steamid = token_steamid(token)

    result = client._pre_login()
    if result != EResult.OK:
        return result

    message = MsgProto(EMsg.ClientLogon)
    # Настоящий steamid, а не пустой шаблон: при входе по токену CM
    # определяет аккаунт по заголовку, имя в теле не передаётся.
    message.header.steamid = SteamID(steamid)
    message.body.protocol_version = 65580
    message.body.client_package_version = 1561159470
    message.body.client_os_type = EOSType.Windows10
    message.body.client_language = "english"
    message.body.should_remember_password = True
    message.body.supports_rate_limit_response = True
    message.body.chat_mode = client.chat_mode
    message.body.obfuscated_private_ip.v4 = 0
    message.body.access_token = token
    # account_name и password НЕ ставятся: см. заголовок модуля.

    client.send(message)
    resp = client.wait_msg(EMsg.ClientLogOnResponse, timeout=timeout)
    return EResult(resp.body.eresult) if resp else EResult.Fail
