"""OpenAI и любые OpenAI-совместимые API (DeepSeek, OpenRouter, Ollama, LM Studio) через base_url."""

from __future__ import annotations

from typing import Any

import openai

from core.llm.base import Content, Image, LLMError, LLMProvider, LLMResponse


def _content(content: Content) -> Any:
    if isinstance(content, str):
        return content
    return [
        {"type": "image_url", "image_url": {"url": f"data:{part.mime};base64,{part.base64()}"}}
        if isinstance(part, Image)
        else {"type": "text", "text": part}
        for part in content
    ]


class OpenAIProvider(LLMProvider):
    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.client = openai.OpenAI(
            api_key=self.api_key() or "not-needed",
            base_url=config.get("base_url"),
            timeout=config.get("timeout", 600),
        )

    def complete(self, system, messages, json_schema=None) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *({**m, "content": _content(m["content"])} for m in messages)],
        }
        # json_object поддерживают почти все совместимые API; схема при этом передаётся в тексте промпта.
        if json_schema is not None and self.config.get("json_mode", True):
            params["response_format"] = {"type": "json_object"}
        # Любые дополнительные параметры конкретного API (temperature, max_completion_tokens, ...).
        params.update(self.config.get("extra_params") or {})

        try:
            response = self.client.chat.completions.create(**params)
        except openai.AuthenticationError as e:
            raise LLMError(f"[{self.name}] неверный API-ключ") from e
        except openai.NotFoundError as e:
            raise LLMError(f"[{self.name}] модель или адрес не найдены: {e.message}") from e
        except openai.RateLimitError as e:
            raise LLMError(f"[{self.name}] превышен лимит запросов: {e.message}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"[{self.name}] ошибка API {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"[{self.name}] нет соединения с {self.config.get('base_url', 'OpenAI')}") from e

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError(f"[{self.name}] ответ обрезан по лимиту токенов — увеличьте лимит в extra_params")
        usage = response.usage
        return LLMResponse(
            text=choice.message.content or "",
            model=response.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )
