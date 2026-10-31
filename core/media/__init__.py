"""Генераторы картинок и видео по секции media в config.yaml — устроено так же, как core/llm."""

from __future__ import annotations

import importlib
from typing import Any

from core.media.base import ImageProvider, Media, MediaError, MediaProvider, VideoProvider

# type из media.providers → класс. SDK импортируются лениво: google-genai не нужен, если используете только OpenAI.
MEDIA_TYPES = {
    "openai_image": "core.media.openai_media:OpenAIImage",
    "google_image": "core.media.google_media:GoogleImage",
    "google_video": "core.media.google_media:GoogleVideo",
    "mock_image": "core.media.mock_media:MockImage",
    "mock_video": "core.media.mock_media:MockVideo",
}


def create_media_provider(media_config: dict[str, Any], name: str | None, kind: str) -> MediaProvider:
    """kind — "image" или "video"; name не задан → media.default_image / media.default_video."""
    providers = media_config.get("providers") or {}
    name = name or media_config.get(f"default_{kind}")
    if name not in providers:
        raise MediaError(f"Генератор '{name}' не найден в media.providers в config.yaml. Доступны: {', '.join(providers)}")

    config = providers[name]
    type_ = config.get("type")
    if type_ not in MEDIA_TYPES:
        raise MediaError(f"[{name}] неизвестный type '{type_}'. Поддерживаются: {', '.join(MEDIA_TYPES)}")

    module_path, class_name = MEDIA_TYPES[type_].split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    if cls.kind != kind:
        raise MediaError(f"[{name}] это генератор {'картинок' if cls.kind == 'image' else 'видео'}, а здесь нужен генератор {'картинок' if kind == 'image' else 'видео'}")
    return cls(name, config)


__all__ = ["ImageProvider", "MEDIA_TYPES", "Media", "MediaError", "MediaProvider", "VideoProvider", "create_media_provider"]
