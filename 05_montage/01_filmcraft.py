"""Монтаж в FilmCraft: 02_storyboard + 03_images + 04_voice → проект FilmCraft и 05_montage/video.mp4.

Альтернатива 01_ffmpeg.py — выбирается в config.yaml: 05_montage → steps: {ffmpeg: false, filmcraft: true}.
Ещё не реализовано.
"""

from core.step import StepContext

OUTPUT = "video.mp4"


def run(ctx: StepContext) -> None:
    raise NotImplementedError("монтаж в FilmCraft ещё не реализован")
