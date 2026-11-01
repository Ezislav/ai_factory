"""Монтаж в FilmCraft: 02_storyboard + 03_visual + 04_audio → проект 05_montage/video.fcproj, который автор открывает и правит сам.

Альтернатива 01_ffmpeg.py — выбирается в config.yaml: 05_montage → steps: {ffmpeg: false, filmcraft: true}.
Время кадров, камера и переходы — те же, что в 01_ffmpeg.py, но это обычные клипы, ключевые кадры и переходы на дорожках
FilmCraft: дорожка V1 — картинки и видео, A1 — озвучка, по клипу на кадр. Готовый ролик автор выводит из самой программы.

Ручные правки в проекте программа не трогает: пока не изменились картинки, озвучка, раскадровка или настройки,
проект не пересобирается, даже при --only 5. Если они изменились, собирается новый проект, а прежний вместе с правками
автора уходит в 05_montage/old. Проект ссылается на файлы ролика по полному пути: после переноса папки
программа попросит найти их заново.
Правки из 05_montage/redo.txt меняют камеру и переходы в раскадровке, поэтому тоже пересобирают проект.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path

from core.pipeline import load_step
from core.step import PipelineError, StepContext

montage = load_step("montage", "ffmpeg")

OUTPUT = "video.fcproj"
REDO_HELP = """# Правки к монтажу: движение камеры и переходы. Пишите обычным языком, одна строка — одна правка:
#   5: вместо наезда отъезд
#   12: переход наплывом
# ВАЖНО: правка отсюда соберёт проект заново, а прежний вместе с вашими ручными правками уйдёт в папку old.
# Если вы уже монтируете в FilmCraft, меняйте камеру и переходы прямо в программе.
# Строки с # в начале не считаются.
"""
STATE = "filmcraft.json"  # из чего собран проект — чтобы заметить новую картинку или настройку
BUILD = "filmcraft_build"  # проект, пока он собирается
BUILD_VERSION = 1  # изменился способ сборки проекта
INSTALLED = Path(r"C:\Program Files\FilmCraft\filmcraft-cli.exe")
TICKS = 254016000000  # единиц времени FilmCraft в секунде
TRANSITION = {"fade": "cross_dissolve", "fadeblack": "dip_to_black"}  # имена переходов ffmpeg → FilmCraft

log = logging.getLogger(__name__)


def find_cli(ctx: StepContext) -> str:
    found = shutil.which("filmcraft-cli") or (str(INSTALLED) if INSTALLED.exists() else None)
    if not found:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] не найден FilmCraft: нет {INSTALLED}. Установите программу или добавьте filmcraft-cli в PATH")
    return found


def fingerprint(ctx: StepContext) -> str:
    settings, clips, _ = montage.build_plan(ctx)
    audio = montage.read_voice(ctx)
    sound = montage.file_hash(ctx.path(audio.track, "audio")) if audio else ""
    shots = [
        (clip.shot, clip.frames, clip.overlap, clip.xfade, clip.camera, clip.is_video, [montage.file_hash(path) for path in clip.sources])
        for clip in clips
    ]
    return montage.make_key(BUILD_VERSION, settings.width, settings.height, settings.fps, settings.camera_zoom, shots, sound)


def built(ctx: StepContext) -> bool:
    """Проект есть и собран из нынешних картинок, озвучки и настроек."""
    state = ctx.path(STATE)
    if not ctx.path(OUTPUT).exists() or not state.exists():
        return False
    return json.loads(state.read_text(encoding="utf-8")).get("fingerprint") == fingerprint(ctx)


def done(ctx: StepContext) -> bool:
    if ctx.redo_lines():
        return False
    try:
        return built(ctx)
    except PipelineError:
        return False


def camera_moves(camera: str, fill: float, zoom: float, width: int, height: int) -> tuple[float, list[tuple[str, object, object]]]:
    """Движение камеры → (постоянный размер картинки, [(параметр, значение в начале, значение в конце)])."""
    centre = [width / 2, height / 2]
    across, down = width * (zoom - 1) / 2, height * (zoom - 1) / 2  # на столько картинка может сдвинуться, не открыв края
    if camera == "zoom_in":
        return fill, [("scale", fill, fill * zoom)]
    if camera == "zoom_out":
        return fill, [("scale", fill * zoom, fill)]
    if camera == "pan_right":  # камера вправо — картинка влево
        return fill * zoom, [("position", [centre[0] + across, centre[1]], [centre[0] - across, centre[1]])]
    if camera == "pan_left":
        return fill * zoom, [("position", [centre[0] - across, centre[1]], [centre[0] + across, centre[1]])]
    if camera == "pan_down":
        return fill * zoom, [("position", [centre[0], centre[1] + down], [centre[0], centre[1] - down])]
    if camera == "pan_up":
        return fill * zoom, [("position", [centre[0], centre[1] - down], [centre[0], centre[1] + down])]
    return fill, []


def build(ctx: StepContext, cli: str, settings, clips: list, audio, project: Path) -> None:
    """Номера материалов и клипов выдаёт сам FilmCraft, поэтому проект собирается в три приёма."""

    def commands(steps: list[dict]) -> list:
        script = project.with_name("commands.jsonl")
        script.write_text("".join(json.dumps(step, ensure_ascii=False) + "\n" for step in steps), encoding="utf-8")
        target = ["--project", str(project), "--save"] if project.exists() else ["--save-as", str(project)]
        result = subprocess.run([cli, *target, "--compact", "run", str(script)], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace")
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        failed = [line for line in lines if not line.get("ok")]
        if result.returncode != 0 or failed or len(lines) != len(steps):
            details = failed[0].get("error") if failed else result.stderr.strip()[-300:]
            raise PipelineError(f"[{ctx.module}.{ctx.step}] FilmCraft не собрал проект: {details or 'код ' + str(result.returncode)}")
        return [line.get("result") for line in lines]

    sounds = {shot.id: ctx.path(shot.file, "audio") for shot in audio.shots} if audio else {}
    sounds = {clip.shot: sounds[clip.shot] for clip in clips if clip.shot in sounds}
    files = list(dict.fromkeys([path for clip in clips for path in clip.sources] + list(sounds.values())))
    _, imported, _ = commands([
        {"id": "file.newProject", "params": {"name": ctx.project.dir.name}},
        {"id": "file.import", "params": {"paths": [str(path) for path in files]}},
        {"id": "file.newSequence", "params": {"name": "Ролик", "width": settings.width, "height": settings.height, "fps": settings.fps, "video": 2, "audio": 2}},
    ])
    if imported["errors"] or len(imported["items"]) != len(files):
        raise PipelineError(f"[{ctx.module}.{ctx.step}] FilmCraft не принял файлы: {imported['errors']}")
    items = dict(zip(files, imported["items"]))

    # Кадры встык: у стыка с переходом запас в clip.frames (см. 01_ffmpeg.py) не нужен, переход сделает FilmCraft.
    ticks = TICKS // settings.fps
    placing, parts = [], []
    start = 0
    for index, clip in enumerate(clips):
        following = clips[index + 1].overlap if index + 1 < len(clips) else 0
        length = clip.frames - clip.overlap // 2 - following // 2
        count = len(clip.sources)
        for part, source in enumerate(clip.sources):
            frames = round(length * (part + 1) / count) - round(length * part / count)
            first = start + round(length * part / count)
            placing.append({"id": "timeline.place", "params": {"item": items[source], "track": "V1", "frame": first, "duration": frames * ticks, "audio": False}})
            parts.append((clip, part, first, frames))
        if clip.shot in sounds:
            placing.append({"id": "timeline.place", "params": {"item": items[sounds[clip.shot]], "track": "A1", "frame": start, "video": False}})
        start += length
    placed = commands(placing)

    video_clips = [result["clips"][0] for step, result in zip(placing, placed) if step["params"]["track"] == "V1"]
    filled = commands([{"id": "clip.fillFrame", "params": {"clips": video_clips}}])[0]["clips"]

    finishing = []
    for (clip, part, first, frames), number, fit in zip(parts, video_clips, filled):
        if not clip.is_video:
            scale, moves = camera_moves(clip.camera, fit["scale"], settings.camera_zoom, settings.width, settings.height)
            if scale != fit["scale"]:
                finishing.append({"id": "effects.setParam", "params": {"clip": number, "effect": "motion", "param": "scale", "value": scale}})
            for param, begin, end in moves:
                for frame, value in ((first, begin), (first + max(frames - 1, 1), end)):
                    finishing.append({"id": "effects.setParam", "params": {"clip": number, "effect": "motion", "param": param, "value": value, "time": frame * ticks, "keyframe": True}})
        if clip.xfade and part == 0:
            finishing.append({"id": "sequence.applyVideoTransition", "params": {"clip": number, "effect": TRANSITION[clip.xfade], "frames": clip.overlap, "edge": "in"}})
    if finishing:
        commands(finishing)


def run(ctx: StepContext) -> None:
    cli = find_cli(ctx)
    if ctx.redo_lines():
        montage.apply_redo(ctx)
        ctx.clear_redo(REDO_HELP)
    path = ctx.path(OUTPUT)
    if built(ctx):
        log.info("[%s.%s] ничего не изменилось, проект с вашими правками оставлен как есть: %s", ctx.module, ctx.step, path)
        return

    settings, clips, complete = montage.build_plan(ctx)
    audio = montage.read_voice(ctx)
    sound = "со звуком" if audio else "без звука — модуль audio выключен"
    log.info("[%s.%s] кадров: %d, %dx%d, %d к/с; %s", ctx.module, ctx.step, len(clips), settings.width, settings.height, settings.fps, sound)
    if not complete:
        log.warning("[%s.%s] пробная сборка: картинки есть не для всех кадров. Чтобы сделать все, поставьте limit: 0 в 03_visual", ctx.module, ctx.step)

    folder = ctx.path(BUILD)
    shutil.rmtree(folder, ignore_errors=True)  # остатки оборванной сборки
    folder.mkdir(parents=True)
    build(ctx, cli, settings, clips, audio, folder / OUTPUT)
    if path.exists():
        ctx.keep_old(path)
        log.warning("[%s.%s] материалы изменились, проект собран заново. Прежний проект с вашими правками — в папке old", ctx.module, ctx.step)
    (folder / OUTPUT).replace(path)
    shutil.rmtree(folder, ignore_errors=True)
    ctx.write(STATE, json.dumps({"fingerprint": fingerprint(ctx), "complete": complete}, indent=2))
    log.info("[%s.%s] проект: %s — откройте его в FilmCraft", ctx.module, ctx.step, path)
