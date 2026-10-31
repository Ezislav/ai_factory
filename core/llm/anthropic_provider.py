"""Claude через официальный Anthropic SDK."""

from __future__ import annotations

from typing import Any

import anthropic

from core.llm.base import Content, Image, LLMError, LLMProvider, LLMResponse

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _content(content: Content) -> Any:
    if isinstance(content, str):
        return content
    return [
        {"type": "image", "source": {"type": "base64", "media_type": part.mime, "data": part.base64()}}
        if isinstance(part, Image)
        else {"type": "text", "text": part}
        for part in content
    ]


class AnthropicProvider(LLMProvider):
    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        # api_key=None → SDK сам возьмёт ANTHROPIC_API_KEY или профиль `ant auth login`.
        self.client = anthropic.Anthropic(api_key=self.api_key(), timeout=config.get("timeout", 600))

    @property
    def model(self) -> str:
        return self.config.get("model", DEFAULT_MODEL)

    def complete(self, system, messages, json_schema=None) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.get("max_tokens", 32000),
            "system": system,
            "messages": [{**m, "content": _content(m["content"])} for m in messages],
        }
        output_config: dict[str, Any] = {}
        if self.config.get("effort"):
            output_config["effort"] = self.config["effort"]
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            params["output_config"] = output_config

        try:
            # Стриминг — чтобы длинные ответы не упирались в HTTP-таймаут.
            if self.config.get("fallbacks"):
                stream = self.client.beta.messages.stream(
                    **params, betas=[FALLBACK_BETA], fallbacks=self.config["fallbacks"]
                )
            else:
                stream = self.client.messages.stream(**params)
            with stream as s:
                message = s.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError(f"[{self.name}] неверный API-ключ (ANTHROPIC_API_KEY)") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"[{self.name}] модель '{self.model}' не найдена") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"[{self.name}] превышен лимит запросов: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"[{self.name}] ошибка API {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"[{self.name}] нет соединения с API Anthropic") from e

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"[{self.name}] модель отказалась отвечать (категория: {category})")
        if message.stop_reason == "max_tokens":
            raise LLMError(f"[{self.name}] ответ обрезан по max_tokens — увеличьте max_tokens в llm.providers в config.yaml")

        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
