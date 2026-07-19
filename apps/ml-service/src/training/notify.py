"""Telegram-уведомления о ходе обучения Win Probability.

Собирает краткую сводку из реестра моделей (production-версия, метрики
на про-эталоне, разрыв датасета, последние кандидаты) и шлёт её в
Telegram. Используется:
- разово: python -m training.notify  (тест/cron);
- из auto-train: notifier.on_retrain(...) после каждого переобучения.

Секреты — только из окружения, НЕ из кода/репозитория:
  TELEGRAM_BOT_TOKEN — токен бота (@BotFather);
  TELEGRAM_CHAT_ID   — id чата (если не задан, определяется из getUpdates
                       по последнему написавшему боту — достаточно один раз
                       отправить боту /start).
"""
from __future__ import annotations

import html
import logging
import os

import requests

from registry import registry_from_env

logger = logging.getLogger("notify")

MODEL = "win_probability"
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

    # -- сводки ----------------------------------------------------------------

    def summary(self, dataset_matches: int | None = None) -> str:
        """Текущий статус production-модели и разрыв датасета."""
        reg = registry_from_env()
        prod = reg.stage_metadata(MODEL)
        versions = reg.list_versions(MODEL)
        lines = ["<b>Manta · Win Probability</b>"]
        if prod:
            m = prod["metrics"]
            lines += [
                f"production: <code>{prod['registry_version']}</code>",
                f"Brier эталон (pro): <b>{m.get('brier_benchmark_pro', '—')}</b>"
                f" (цель ≤ 0.18)",
                f"обучена на {prod['dataset']['matches']} матчах",
            ]
        if dataset_matches is not None and prod:
            gap = dataset_matches - prod["dataset"]["matches"]
            lines.append(f"датасет сейчас: {dataset_matches} ({gap:+d} к prod)")
        lines.append(f"версий в реестре: {len(versions)}")
        return "\n".join(lines)

    def on_retrain(self, new_metrics: dict, promoted: bool, reason: str,
                   dataset_matches: int) -> bool:
        """Уведомление о завершённом переобучении."""
        bm = new_metrics.get("brier_benchmark_pro", "—")
        val = new_metrics.get("brier_calibrated", "—")
        oof = new_metrics.get("brier_oof", "—")
        phases = " / ".join(str(new_metrics.get(f"brier_{p}", "—"))
                            for p in ("early", "mid", "late"))
        icon = "✅ продвинута в production" if promoted else "⏸ отклонена гейтом"
        # reason приходит из should_promote и содержит '<='/'>' — экранируем,
        # иначе parse_mode=HTML в Telegram отдаёт 400 (символы как теги).
        text = (
            f"<b>Manta · переобучение завершено</b>\n"
            f"{icon}\n"
            f"датасет: {dataset_matches} матчей\n"
            f"Brier эталон (pro): <b>{bm}</b>  ·  валидация: {val}  ·  OOF: {oof}\n"
            f"по фазам (0–10/10–25/25+ мин): {phases}\n"
            f"гейт: {html.escape(str(reason))}"
        )
        return self.send(text)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    n = TelegramNotifier()
    if not n.enabled:
        print("TELEGRAM_BOT_TOKEN не задан")
        return 1
    ok = n.send(n.summary())
    print("отправлено" if ok else "не отправлено (см. лог / отправьте боту /start)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
