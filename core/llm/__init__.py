"""Выбор нейросети по секции llm в config.yaml."""

from __future__ import annotations

import importlib
from typing import Any

from core.llm.base import Image, LLMError, LLMProvider, LLMResponse, interruptible

# type из llm.providers → класс. SDK импортируются лениво: openai не нужен, если используете только Claude.
PROVIDER_TYPES = {
    "anthropic": "core.llm.anthropic_provider:AnthropicProvider",
    "openai": "core.llm.openai_provider:OpenAIProvider",
    "mock": "core.llm.mock_provider:MockProvider",
}


def create_provider(llm_config: dict[str, Any], name: str | None = None) -> LLMProvider:
    providers = llm_config.get("providers") or {}
    name = name or llm_config.get("default")
    if name not in providers:
        raise LLMError(f"Нейросеть '{name}' не найдена в llm.providers в config.yaml. Доступны: {', '.join(providers)}")

    config = providers[name]
    type_ = config.get("type")
    if type_ not in PROVIDER_TYPES:
        raise LLMError(f"[{name}] неизвестный type '{type_}'. Поддерживаются: {', '.join(PROVIDER_TYPES)}")

    module_path, class_name = PROVIDER_TYPES[type_].split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    return cls(name, config)


__all__ = ["interruptible", "Image", "LLMError", "LLMProvider", "LLMResponse", "PROVIDER_TYPES", "create_provider"]
