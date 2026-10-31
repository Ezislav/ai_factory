"""Картинки через OpenAI Images API (gpt-image)."""

from __future__ import annotations

import base64
import urllib.request
from typing import Any

import openai

from core.media.base import ImageProvider, Media, MediaError, detect_extension

# Размеры, которые понимает gpt-image; монтаж потом подгонит картинку под формат ролика.
SIZES = {"16:9": "1536x1024", "3:2": "1536x1024", "9:16": "1024x1536", "2:3": "1024x1536", "1:1": "1024x1024"}


class OpenAIImage(ImageProvider):
    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.client = openai.OpenAI(api_key=self.api_key(), base_url=config.get("base_url"), timeout=config.get("timeout", 300))

    def generate(self, prompt: str, aspect_ratio: str) -> Media:
        params: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": self.config.get("size") or SIZES.get(aspect_ratio, "auto"),
            "n": 1,
        }
        if self.config.get("quality"):
            params["quality"] = self.config["quality"]
        params.update(self.config.get("extra_params") or {})

        try:
            response = self.client.images.generate(**params)
        except openai.AuthenticationError as e:
            raise MediaError(f"[{self.name}] неверный API-ключ", fatal=True) from e
        except openai.NotFoundError as e:
            raise MediaError(f"[{self.name}] модель не найдена — проверьте model в config.yaml: {e.message}", fatal=True) from e
        except openai.BadRequestError as e:
            # Сюда же попадает отказ модерации — описание кадра стоит смягчить.
            raise MediaError(f"[{self.name}] запрос отклонён: {e.message}", retry=False) from e
        except openai.APIStatusError as e:
            raise MediaError(f"[{self.name}] ошибка API {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise MediaError(f"[{self.name}] нет соединения с API: {e}") from e

        item = response.data[0] if response.data else None
        if item and item.b64_json:
            data = base64.b64decode(item.b64_json)
        elif item and item.url:
            with urllib.request.urlopen(item.url, timeout=120) as f:
                data = f.read()
        else:
            raise MediaError(f"[{self.name}] пустой ответ")
        return Media(data=data, extension=detect_extension(data, ".png"), model=self.model)
