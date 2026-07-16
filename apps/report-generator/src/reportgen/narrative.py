"""LLM-нарратив разбора матча (Гл. 3.8/3.9, спринт 28).

Заменяет шаблонный бейзлайн build_narrative() генерацией через Claude API,
когда задан ANTHROPIC_API_KEY. Деградация — по Гл. 3.9 («LLM Service
недоступен → показать метрики без нарратива»): любая ошибка или отсутствие
ключа возвращают None, и билдер использует шаблон.

Guardrails: промпт передаёт ТОЛЬКО факты, уже вычисленные билдером
(победитель, переломный момент, оценки игроков, ошибки), и запрещает
модели придумывать события сверх них — фактчек «на входе», а не пост-хок.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("reportgen.narrative")

DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """Ты — AI-аналитик Dota 2 платформы Manta. Пишешь короткий
разбор матча для игрока по фактам из статистического отчёта.

Правила:
- Используй ТОЛЬКО факты из переданного отчёта. Ничего не выдумывай:
  ни событий, ни предметов, ни драфта — их нет в данных.
- 3–5 предложений сплошным текстом, без заголовков, списков и markdown.
- Пиши по-русски, живо, но без пафоса. Имена героев — по-английски
  (Axe, Kez), без префикса npc_dota_hero_.
- Числа округляй до целых процентов; вероятности — это шансы Radiant.
- Если есть переломный момент — объясни его роль. Отметь лучшего и
  худшего по impact_score игрока и критические смерти (errors)."""


def _facts(winner: str, final_wp: float, turning: dict | None,
           players: list[dict]) -> str:
    """Компактная фактологическая сводка для промпта."""
    lines = [f"Победитель: {winner}.",
             f"Итоговая вероятность победы Radiant: {final_wp:.2f}."]
    if turning:
        lines.append(
            f"Переломный момент: минута {turning['game_time'] // 60}, "
            f"сдвиг вероятности {turning['delta_wp']:+.2f} "
            f"({'в пользу Radiant' if turning['delta_wp'] > 0 else 'в пользу Dire'}).")
    for p in players:
        hero = str(p.get("hero", "")).replace("npc_dota_hero_", "")
        team = "Radiant" if int(p.get("player_id", 0)) < 5 else "Dire"
        errs = p.get("errors") or []
        err_txt = ""
        if errs:
            worst = max(errs, key=lambda e: abs(e.get("delta_wp", 0)))
            err_txt = (f"; критических смертей: {len(errs)}, худшая на "
                       f"минуте {int(worst.get('game_time', 0)) // 60} "
                       f"(ΔWP {worst.get('delta_wp', 0):+.2f})")
        lines.append(
            f"- {p.get('player_name') or hero} ({hero}, {team}, "
            f"линия {p.get('lane') or '?'}): laning {p.get('laning_score', 0):.1f}/10, "
            f"impact {p.get('impact_score', 0):.1f}/10, GPM {p.get('gpm', 0):.0f}"
            f"{err_txt}")
    return "\n".join(lines)


class LLMNarrator:
    """Генератор нарратива через Anthropic SDK; отключён без ключа."""

    def __init__(self) -> None:
        self.model = os.getenv("NARRATIVE_MODEL", DEFAULT_MODEL)
        self._client = None
        if os.getenv("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                self._client = anthropic.Anthropic(
                    timeout=float(os.getenv("NARRATIVE_TIMEOUT_S", "30")),
                    max_retries=1)
            except Exception:  # noqa: BLE001 — нет пакета ⇒ работаем шаблоном
                logger.exception("anthropic SDK unavailable, falling back")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def generate(self, winner: str, final_wp: float, turning: dict | None,
                 players: list[dict]) -> str | None:
        """Нарратив по фактам отчёта; None ⇒ деградация на шаблон."""
        if self._client is None:
            return None
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content":
                           "Факты матча:\n" +
                           _facts(winner, final_wp, turning, players) +
                           "\n\nНапиши разбор."}])
            if resp.stop_reason == "refusal":
                logger.warning("narrative refused: %s", resp.stop_details)
                return None
            text = "".join(b.text for b in resp.content
                           if b.type == "text").strip()
            return text or None
        except Exception:  # noqa: BLE001 — отчёт важнее нарратива
            logger.exception("LLM narrative failed, falling back to template")
            return None
