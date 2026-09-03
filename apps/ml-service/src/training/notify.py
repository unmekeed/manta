"""Telegram-сводки о ходе обучения Win Probability.

Собирает краткую сводку из реестра моделей (production-версия, метрики
на про-эталоне, разрыв датасета, последние кандидаты) и шлёт её в
Telegram. Используется:
- разово: python -m training.notify  (тест/cron);
- из auto-train: notifier.on_retrain(...) после каждого переобучения.

САМА ОТПРАВКА живёт в libs/manta_notify.py (спринт 191). Здесь остались
только сводки, знающие про реестр моделей. Разделение понадобилось, когда
кричать понадобилось и report-generator'у: тянуть в него ml-service ради
одного POST в Telegram нельзя, а вторая копия отправки разъехалась бы с
первой при первой же правке.

Секреты — только из окружения, НЕ из кода/репозитория:
  TELEGRAM_BOT_TOKEN — токен бота (@BotFather);
  TELEGRAM_CHAT_ID   — id чата (если не задан, определяется из getUpdates
                       по последнему написавшему боту — достаточно один раз
                       отправить боту /start).
"""
from __future__ import annotations

import html
import logging

from manta_notify import TelegramNotifier as _Transport

from registry import registry_from_env

logger = logging.getLogger("notify")

MODEL = "win_probability"


class TelegramNotifier(_Transport):
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
