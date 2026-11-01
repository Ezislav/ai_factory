"""Видео из картинок: кадры с media: video → 03_visual/1.mp4, ... и список videos.json.

Видео начинается с уже сгенерированной картинки кадра (01_generate), поэтому стиль ролика не ломается.
Какие кадры будут видео, решает раскадровка: media: video (лимит — video_shots в 02_storyboard в config.yaml),
можно поправить вручную в 02_storyboard/storyboard.json. Кадров с видео нет — шаг ничего не тратит.
Как и картинки, готовое видео второй раз не генерируется, если кадр не поменялся — чтобы переделать, удалите его файл.
"""

import logging

from pydantic import BaseModel

from core.pipeline import load_step
from core.step import PipelineError, StepContext

generate = load_step("visual", "generate")

OUTPUT = "videos.json"

CAMERA = {
    "static": "static camera",
    "zoom_in": "slow push-in",
    "zoom_out": "slow pull-back",
    "pan_left": "slow pan to the left",
    "pan_right": "slow pan to the right",
    "pan_up": "slow tilt up",
    "pan_down": "slow tilt down",
}

log = logging.getLogger(__name__)


class ShotVideo(BaseModel):
    id: int
    file: str
    prompt: str


class VideoSet(BaseModel):
    shots: list[ShotVideo]


def run(ctx: StepContext) -> None:
    board = generate.read_storyboard(ctx)
    shots = [shot for shot in board.shots if shot.media == "video"]
    if not shots:
        log.info("[%s.%s] в раскадровке нет кадров с media: video — видео не нужно", ctx.module, ctx.step)
        ctx.write_json(OUTPUT, VideoSet(shots=[]))
        return

    images = ctx.read_json(generate.OUTPUT, generate.ImageSet)
    if images is None:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет {generate.OUTPUT} — картинки делает шаг generate")
    image_files = {shot.id: shot.files[0] for shot in images.shots}  # у видео-кадра одна картинка

    prompts = {
        shot.id: ctx.render_prompt(
            motion=shot.motion or shot.visual,
            description=(shot.image_prompts or [shot.visual])[0],
            camera=CAMERA[shot.camera],
            style=board.style,
        )[1]
        for shot in shots
    }
    provider = ctx.media("video")
    log.info("[%s.%s] видео: %d, генератор: %s (%s)", ctx.module, ctx.step, len(shots), provider.name, provider.model)

    previous = ctx.read_json(OUTPUT, VideoSet)
    for shot in previous.shots if previous else []:  # кадр поменялся — старое видео в old/, оно сделается заново
        if prompts.get(shot.id) != shot.prompt and ctx.path(shot.file).exists():
            ctx.keep_old(ctx.path(shot.file))

    def job(shot_id: int):
        image = ctx.path(image_files[shot_id]).read_bytes()
        return lambda: provider.animate(image, prompts[shot_id], board.aspect_ratio)

    files = generate.produce(ctx, {str(shot.id): job(shot.id) for shot in shots}, provider, (".mp4",))
    ctx.write_json(OUTPUT, VideoSet(shots=[ShotVideo(id=shot.id, file=files[str(shot.id)], prompt=prompts[shot.id]) for shot in shots]))
