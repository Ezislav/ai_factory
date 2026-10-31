"""Картинки (Imagen) и видео из картинки (Veo) через Google Gemini API, пакет google-genai."""

from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import errors, types

from core.media.base import ImageProvider, Media, MediaError, VideoProvider, detect_extension, mime_type


def _client(provider: ImageProvider | VideoProvider) -> genai.Client:
    return genai.Client(api_key=provider.api_key())


def _api_error(name: str, e: errors.APIError) -> MediaError:
    if e.code in (401, 403):
        return MediaError(f"[{name}] нет доступа — проверьте ключ и что API включён: {e.message}", fatal=True)
    if e.code == 404:
        return MediaError(f"[{name}] модель не найдена — проверьте model в config.yaml: {e.message}", fatal=True)
    return MediaError(f"[{name}] ошибка API {e.code}: {e.message}", retry=e.code != 400)


class GoogleImage(ImageProvider):
    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.client = _client(self)

    def generate(self, prompt: str, aspect_ratio: str) -> Media:
        config = types.GenerateImagesConfig(number_of_images=1, aspect_ratio=aspect_ratio, **(self.config.get("extra_params") or {}))
        try:
            response = self.client.models.generate_images(model=self.model, prompt=prompt, config=config)
        except errors.APIError as e:
            raise _api_error(self.name, e) from e

        images = response.generated_images or []
        if not images or not images[0].image or not images[0].image.image_bytes:
            reason = images[0].rai_filtered_reason if images else None
            raise MediaError(f"[{self.name}] картинка не получена — сработал фильтр безопасности{f': {reason}' if reason else ''}", retry=False)
        data = images[0].image.image_bytes
        return Media(data=data, extension=detect_extension(data, ".png"), model=self.model)


class GoogleVideo(VideoProvider):
    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.client = _client(self)

    def animate(self, image: bytes, prompt: str, aspect_ratio: str) -> Media:
        config: dict[str, Any] = {"number_of_videos": 1, "aspect_ratio": aspect_ratio}
        for key in ("duration_seconds", "resolution", "generate_audio"):
            if self.config.get(key) is not None:
                config[key] = self.config[key]
        config.update(self.config.get("extra_params") or {})

        try:
            operation = self.client.models.generate_videos(
                model=self.model,
                prompt=prompt,
                image=types.Image(image_bytes=image, mime_type=mime_type(image)),
                config=types.GenerateVideosConfig(**config),
            )
            # Видео делается минуты: опрашиваем, пока не будет готово.
            deadline = time.monotonic() + self.config.get("timeout", 900)
            while not operation.done:
                if time.monotonic() > deadline:
                    raise MediaError(f"[{self.name}] видео не готово за {self.config.get('timeout', 900)} с")
                time.sleep(self.config.get("poll_seconds", 10))
                operation = self.client.operations.get(operation)
        except errors.APIError as e:
            raise _api_error(self.name, e) from e

        if operation.error:
            raise MediaError(f"[{self.name}] видео не сделано: {operation.error}")
        response = operation.response
        videos = response.generated_videos if response else None
        if not videos or not videos[0].video:
            reasons = "; ".join(response.rai_media_filtered_reasons or []) if response else ""
            raise MediaError(f"[{self.name}] видео не получено — сработал фильтр безопасности{f': {reasons}' if reasons else ''}", retry=False)
        data = videos[0].video.video_bytes or self.client.files.download(file=videos[0].video)
        if not data:
            raise MediaError(f"[{self.name}] не удалось скачать видео")
        return Media(data=data, extension=detect_extension(data, ".mp4"), model=self.model)
