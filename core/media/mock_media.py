"""Генераторы-заглушки без сети и ключей: однотонная картинка, пустое «видео» и писк вместо речи. Для проверки пайплайна."""

from __future__ import annotations

import hashlib
import io
import math
import struct
import wave
import zlib

from core.media.base import AudioProvider, ImageProvider, Media, VideoProvider

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


class MockAudio(AudioProvider):
    @property
    def model(self) -> str:
        return "mock"

    def speak(self, text: str) -> Media:
        rate = 16000
        samples = int(rate * max(0.5, len(text.split()) * 60 / 140))  # длина как у речи в темпе 140 слов в минуту
        tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(samples))
        data = io.BytesIO()
        with wave.open(data, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(tone)
        return Media(data=data.getvalue(), extension=".wav", model="mock")
