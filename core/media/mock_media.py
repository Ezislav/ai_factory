"""Генераторы-заглушки без сети и ключей: однотонная картинка и пустое «видео». Для проверки пайплайна."""

from __future__ import annotations

import hashlib
import struct
import zlib

from core.media.base import ImageProvider, Media, VideoProvider

SIZES = {"16:9": (320, 180), "9:16": (180, 320), "1:1": (240, 240)}


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class MockImage(ImageProvider):
    @property
    def model(self) -> str:
        return "mock"

    def generate(self, prompt: str, aspect_ratio: str) -> Media:
        rgb = tuple(hashlib.md5(prompt.encode()).digest()[:3])  # у каждого описания свой цвет
        return Media(data=solid_png(*SIZES.get(aspect_ratio, (320, 180)), rgb), extension=".png", model="mock")


class MockVideo(VideoProvider):
    @property
    def model(self) -> str:
        return "mock"

    def animate(self, image: bytes, prompt: str, aspect_ratio: str) -> Media:
        return Media(data=b"mock video: " + prompt.encode(), extension=".mp4", model="mock")  # не настоящий mp4
