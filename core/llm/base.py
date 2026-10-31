"""Общий интерфейс LLM-провайдера. Модули пайплайна работают только с ним."""

from __future__ import annotations

import base64
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")  # OPENAI_API_KEY, а не sk-...


class LLMError(RuntimeError):
    """Ошибка обращения к нейросети (сеть, ключ, отказ, обрезанный ответ)."""


@dataclass
class Image:
    """Картинка в запросе к нейросети — для проверки сгенерированных изображений."""

    data: bytes
    mime: str = "image/png"

    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


Content = str | list[str | Image]  # содержимое сообщения: текст или текст вперемешку с картинками


def text_of(content: Content) -> str:
    """Только текст сообщения, без картинок."""
    return content if isinstance(content, str) else "\n\n".join(part for part in content if isinstance(part, str))


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(ABC):
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @property
    def model(self) -> str:
        return self.config.get("model", "")

    def api_key(self) -> str | None:
        return api_key_from(self.name, self.config, LLMError)

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict[str, Content]],
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Отправить диалог и вернуть текст ответа.

        messages — список {"role": "user" | "assistant", "content": str или список из str и Image}.
        json_schema — если задана, провайдер включает свой JSON-режим (если он есть).
        """


def interruptible(fn, *args: Any, **kwargs: Any) -> Any:
    """Вызвать fn так, чтобы Ctrl+C срабатывал сразу, а не после ответа сети (в Windows ожидание не прерывается).

    В главном потоке запрос уходит в отдельный поток, а главный ждёт его короткими шагами.
    """
    if threading.current_thread() is not threading.main_thread():
        return fn(*args, **kwargs)
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as e:  # ошибку передаём в главный поток как есть
            result["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(0.2)
    if "error" in result:
        raise result["error"]
    return result["value"]


def api_key_from(name: str, config: dict[str, Any], error: type[Exception] = RuntimeError) -> str | None:
    """Ключ из `api_key` или из переменной окружения `api_key_env` (её значение — в .env)."""
    if config.get("api_key"):
        return config["api_key"]
    env = config.get("api_key_env")
    if not env:
        return None
    if not _ENV_NAME_RE.match(env):
        # Сюда по ошибке вписали сам ключ — не печатаем его.
        raise error(
            f"[{name}] в api_key_env должно быть имя переменной (например, OPENAI_API_KEY), "
            "а похоже, что там сам ключ. Перенесите ключ в .env, а в api_key_env впишите имя"
        )
    value = os.environ.get(env)
    if not value:
        raise error(f"[{name}] не задана переменная окружения {env} (впишите в .env)")
    return value
