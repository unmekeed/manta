"""Опциональный LLM-слой Coach (Гл. 6.3: AI Coach поверх RAG).

Без ключа провайдера каркас работает в шаблонном режиме (plan.render_plan)
— мягкая деградация, никакой функционал не блокируется. С ключом тот же
вход (наблюдения + RAG-контекст) превращается в связный текст тренером.

Провайдер выбирается окружением:
    COACH_LLM_PROVIDER=anthropic   COACH_LLM_API_KEY=...   (модель — env)

Интерфейс одного метода generate(prompt) сознательно минимален: смена
провайдера не трогает вызывающий код.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("coach.llm")

SYSTEM_PROMPT = (
    "Ты — тренер по Dota 2. По наблюдениям анализатора и примерам похожих "
    "матчей составь короткий конкретный план тренировки: что тренировать, "
    "почему (цифры из наблюдений), как именно. Без воды, по-русски.")


class TemplateLLM:
    """Fallback без ключа: возвращает вход как есть (уже отрендеренный план)."""

    name = "template"

    def generate(self, prompt: str) -> str:
        return prompt


class AnthropicLLM:
    """Клиент Anthropic Messages API (подключается только при наличии ключа)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        self._key = api_key
        self._model = model

    def generate(self, prompt: str) -> str:
        import requests
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self._key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self._model, "max_tokens": 1024,
                  "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []))


def llm_from_env():
    provider = os.getenv("COACH_LLM_PROVIDER", "").lower()
    key = os.getenv("COACH_LLM_API_KEY", "")
    if provider == "anthropic" and key:
        model = os.getenv("COACH_LLM_MODEL", "claude-haiku-4-5-20251001")
        logger.info("LLM-слой: anthropic (%s)", model)
        return AnthropicLLM(key, model)
    logger.info("LLM-слой выключен (нет COACH_LLM_PROVIDER/KEY) — шаблонный режим")
    return TemplateLLM()
