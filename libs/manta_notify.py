"""Отправка уведомлений в Telegram — общая для всех сервисов (спринт 191).

ЗАЧЕМ ОБЩАЯ. Раньше отправка жила внутри `ml-service/training/notify.py`
вместе со сводками по реестру моделей. Обучение — единственное, что
умело позвать владельца, и это ровно та причина, по которой отказ
генерации отчётов 2–3 сентября прожил 29 часов молча: сервису, у
которого сломался главный продукт, было нечем кричать.

Здесь ТОЛЬКО транспорт: токен, чат, отправка. Никаких сводок и никакого
реестра — иначе модуль потянул бы в report-generator зависимости
ml-service, которых там нет и быть не должно.

Секреты — только из окружения:
  TELEGRAM_BOT_TOKEN — токен бота (@BotFather);
  TELEGRAM_CHAT_ID   — id чата; если не задан, определяется из getUpdates
                       по последнему написавшему боту.

Отсутствие токена — НЕ ошибка: на машине разработчика уведомления просто
выключены, и `enabled` про это честно говорит. Вызывающий код обязан
проверять `enabled` перед составлением дорогого текста, но не обязан —
`send` сам вернёт False.
"""
from __future__ import annotations

import html
import logging
import os

import requests

logger = logging.getLogger("notify")

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, **params):
        resp = requests.post(API.format(token=self.token, method=method),
                             json=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def resolve_chat_id(self) -> str | None:
        """Если chat_id не задан — взять последний чат из getUpdates
        (пользователь должен был написать боту /start)."""
        if self.chat_id:
            return self.chat_id
        try:
            upd = self._call("getUpdates")
            chats = [u["message"]["chat"]["id"] for u in upd.get("result", [])
                     if "message" in u]
            if chats:
                self.chat_id = str(chats[-1])
                logger.info("chat_id определён из getUpdates: %s", self.chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("не удалось получить chat_id из getUpdates")
        return self.chat_id or None

    def send(self, text: str) -> bool:
        if not self.enabled:
            logger.info("telegram отключён (нет TELEGRAM_BOT_TOKEN)")
            return False
        chat = self.resolve_chat_id()
        if not chat:
            logger.warning("нет chat_id — отправьте боту /start")
            return False
        try:
            self._call("sendMessage", chat_id=chat, text=text,
                       parse_mode="HTML", disable_web_page_preview=True)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("HTML-отправка не прошла, пробую без разметки")
        try:  # fallback: без parse_mode, чтобы не потерять уведомление
            plain = text.replace("<b>", "").replace("</b>", "") \
                        .replace("<code>", "").replace("</code>", "")
            self._call("sendMessage", chat_id=chat, text=html.unescape(plain),
                       disable_web_page_preview=True)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("ошибка отправки в telegram")
            return False
