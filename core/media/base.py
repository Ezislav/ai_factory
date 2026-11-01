"""Общий интерфейс генераторов картинок, видео и речи. Модули пайплайна работают только с ним."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from core.llm.base import api_key_from


class MediaError(RuntimeError):
    """Ошибка генерации картинки, видео или речи (сеть, ключ, фильтр безопасности).

    retry — есть смысл повторить тот же запрос (сбой сети, лимит); fatal — остальные кадры тоже не получатся (ключ).
    """

    def __init__(self, message: str, retry: bool = True, fatal: bool = False):
        super().__init__(message)
        self.retry = retry and not fatal
        self.fatal = fatal


@dataclass
class Media:
    data: bytes
    extension: str  # ".png", ".jpg", ".mp4", ".wav" — по содержимому файла
    model: str


def detect_extension(data: bytes, default: str) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":
        return ".mp4"
    return default


def mime_type(data: bytes) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(detect_extension(data, ""), "image/png")


class MediaProvider(ABC):
    kind = ""  # "image", "video" или "audio"

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @property
    def model(self) -> str:
        return self.config.get("model", "")

    def api_key(self) -> str | None:
        return api_key_from(self.name, self.config, MediaError)


class ImageProvider(MediaProvider):
    kind = "image"

    @abstractmethod
    def generate(self, prompt: str, aspect_ratio: str) -> Media:
        """Картинка по описанию."""


class VideoProvider(MediaProvider):
    kind = "video"

    @abstractmethod
    def animate(self, image: bytes, prompt: str, aspect_ratio: str) -> Media:
        """Видео, которое начинается с картинки image и развивается по описанию prompt."""


class AudioProvider(MediaProvider):
    kind = "audio"

    @abstractmethod
    def speak(self, text: str) -> Media:
        """Текст диктора → речь."""
