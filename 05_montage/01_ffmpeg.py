"""Монтаж через ffmpeg: 02_storyboard + 03_visual + 04_audio → 05_montage/video.mp4.

Время каждого кадра и звук берутся из озвучки (audio.json и voice.wav), картинка оживает движением камеры (camera).
Если модуль 04_audio выключен в pipeline, ролик собирается без звука, со временем кадров из раскадровки.
Несколько картинок в кадре делят его время поровну. Кадр с media: video берёт готовое видео из videos.json;
короткое видео держит последний кадр, длинное обрезается. Переходы (transition) не сдвигают время: наплыв
идёт поровну до и после границы кадров, поэтому картинка совпадает с голосом.

Готовые куски лежат в 05_montage/clips и второй раз не считаются: поправили одну картинку — пересоберётся только
её кадр. Папку clips можно удалять, она сделается заново. После пробного запуска картинок (limit) ролик
можно собрать из готовых кадров: python run.py --project <имя> --only 4,5
Правки из 05_montage/redo.txt меняют камеру и переходы в раскадровке, пересобираются только затронутые кадры.
"""

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from core.pipeline import load_step
from core.step import PipelineError, StepContext

storyboard = load_step("storyboard", "build")
generate = load_step("visual", "generate")
animate = load_step("visual", "animate")
voice = load_step("audio", "tts")

OUTPUT = "video.mp4"
REDO_PROMPT = "01_redo.md"
REDO_HELP = """# Правки к монтажу: движение камеры и переходы. Пишите обычным языком, одна строка — одна правка:
#   5: вместо наезда отъезд
#   12: переход наплывом
#   Убери все переходы через чёрное
# Строки с # в начале не считаются. Настройки всего ролика (качество, размер, сила наезда) — в config.yaml.
"""
CLIPS = "clips"  # готовые куски ролика
STATE = "montage.json"  # из чего собран video.mp4 — чтобы заметить новую картинку или настройку
SCALE = 2  # камера движется по увеличенной картинке — так резче и плавнее
RENDER_VERSION = 2  # изменился способ сборки кадра — старые куски из clips не годятся
XFADE = {"crossfade": "fade", "fade_black": "fadeblack"}

log = logging.getLogger(__name__)


@dataclass
class Clip:
    shot: int
    frames: int  # вместе с запасом на переходы
    sources: list[Path]
    is_video: bool
    camera: str
    xfade: str | None  # переход от предыдущего кадра; None — склейка
    overlap: int = 0  # длина этого перехода в кадрах видео
    key: str = ""

    @property
    def name(self) -> str:
        return f"{self.shot}_{self.key}.mp4"


@dataclass
class Settings:
    width: int
    height: int
    fps: int
    crf: int
    preset: str
    camera_zoom: float


class ShotEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(description="Номер кадра")
    camera: storyboard.Camera = Field(description="Движение камеры")
    transition: storyboard.Transition = Field(description="Переход к этому кадру от предыдущего")


class MontageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[ShotEdit] = Field(description="Только изменившиеся кадры")


def apply_redo(ctx: StepContext) -> None:
    """Правки автора → камера и переходы в раскадровке. Прежний вариант раскадровки уходит в её old/."""
    wishes = ctx.redo_lines()
    if not wishes:
        return
    board = generate.read_storyboard(ctx)
    known = {shot.id for shot in board.shots}

    def check(edit: MontageEdit) -> None:
        unknown = [str(item.id) for item in edit.shots if item.id not in known]
        if unknown:
            raise ValueError(f"нет кадров: {', '.join(unknown)}")

    system, user = ctx.render_prompt(template=REDO_PROMPT, wishes=wishes, shots=[shot.model_dump() for shot in board.shots])
    edit = ctx.ask_json(system, user, MontageEdit, check=check)
    updates = {item.id: {"camera": item.camera, "transition": item.transition} for item in edit.shots}
    changed = board.model_copy(update={"shots": [shot.model_copy(update=updates.get(shot.id, {})) for shot in board.shots]})
    for name in (storyboard.OUTPUT, storyboard.PREVIEW):
        path = ctx.path(name, "storyboard")
        if path.exists():
            ctx.keep_old(path)
    storyboard.save(ctx, changed)
    ctx.clear_redo(REDO_HELP)
    log.info("[%s.%s] правки внесены, изменилось кадров: %d", ctx.module, ctx.step, len(updates))


def read_settings(ctx: StepContext, aspect_ratio: str) -> Settings:
    params = ctx.params
    height = params.get("height", 1080)
    across, down = (float(part) for part in aspect_ratio.split(":"))
    return Settings(
        width=round(height * across / down / 2) * 2,  # кодек принимает только чётные размеры
        height=round(height / 2) * 2,
        fps=params.get("fps", 30),
        crf=params.get("crf", 20),
        preset=params.get("preset", "medium"),
        camera_zoom=params.get("camera_zoom", 1.12),
    )


def file_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def make_key(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:10]


def read_voice(ctx: StepContext):
    """Озвучка ролика; None — модуль выключен в pipeline."""
    if "audio" not in ctx.settings.pipeline:
        return None
    audio = ctx.read_json(voice.OUTPUT, voice.AudioSet, module="audio")
    if audio is None:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет {voice.OUTPUT} — озвучку делает модуль audio. Запустите: --only 4,5")
    return audio


def build_plan(ctx: StepContext) -> tuple[Settings, list[Clip], bool]:
    """Кадры раскадровки → куски ролика. Третье значение — собраны ли все кадры (false — после пробного запуска картинок)."""
    board = generate.read_storyboard(ctx)
    images = ctx.read_json(generate.OUTPUT, generate.ImageSet, module="visual")
    if images is None:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет {generate.OUTPUT} — картинки делает модуль visual")
    videos = ctx.read_json(animate.OUTPUT, animate.VideoSet, module="visual")
    video_files = {shot.id: shot.file for shot in videos.shots} if videos else {}
    image_files = {shot.id: shot.files for shot in images.shots}

    settings = read_settings(ctx, board.aspect_ratio)
    fps = settings.fps
    wanted = round(ctx.params.get("transition_seconds", 0.6) * fps / 2) * 2  # чётное: половина до границы, половина после

    audio = read_voice(ctx)
    times = {shot.id: (shot.start, shot.end) for shot in (audio.shots if audio else board.shots)}

    shots = [shot for shot in board.shots if shot.id in image_files and shot.id in times]
    absent = [shot.id for shot in board.shots if shot.id not in image_files]
    if absent and images.complete:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет картинок для кадров: {', '.join(map(str, absent[:10]))} — запустите модуль visual")
    silent = [shot.id for shot in board.shots if shot.id in image_files and shot.id not in times]
    if silent:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет озвучки для кадров: {', '.join(map(str, silent[:10]))} — запустите модуль audio")
    if not shots:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] в {generate.OUTPUT} нет ни одного кадра")

    # Границы считаются от начала ролика, а не от длительностей — так ошибки округления не накапливаются.
    lengths = [max(1, round(times[shot.id][1] * fps) - round(times[shot.id][0] * fps)) for shot in shots]
    overlaps = [0] * len(shots)  # переход от предыдущего кадра; у первого кадра его нет
    for index in range(1, len(shots)):
        if shots[index].transition in XFADE:
            overlaps[index] = min(wanted, lengths[index - 1] // 2 * 2, lengths[index] // 2 * 2)

    clips = []
    for index, shot in enumerate(shots):
        head = overlaps[index] // 2
        tail = overlaps[index + 1] // 2 if index + 1 < len(shots) else 0
        video = ctx.path(video_files[shot.id], "visual") if shot.id in video_files else None
        is_video = video is not None and video.exists()
        sources = [video] if is_video else [ctx.path(name, "visual") for name in image_files[shot.id]]
        lost = [path.name for path in sources if not path.exists()]
        if lost:
            raise PipelineError(f"[{ctx.module}.{ctx.step}] кадр {shot.id}: нет файлов {', '.join(lost)} — их делает модуль visual")
        clip = Clip(
            shot=shot.id,
            frames=lengths[index] + head + tail,
            sources=sources,
            is_video=is_video,
            camera=shot.camera,
            xfade=XFADE[shot.transition] if overlaps[index] else None,
            overlap=overlaps[index],
        )
        clip.key = make_key(RENDER_VERSION, vars(settings), clip.frames, clip.is_video, clip.camera, [file_hash(path) for path in sources])
        clips.append(clip)
    return settings, clips, not absent


def groups_of(clips: list[Clip]) -> list[list[Clip]]:
    """Кадры, связанные переходами, — вместе; между группами склейка без перекодирования."""
    groups: list[list[Clip]] = []
    for clip in clips:
        if clip.xfade and groups:
            groups[-1].append(clip)
        else:
            groups.append([clip])
    return groups


def group_name(group: list[Clip]) -> str:
    if len(group) == 1:
        return group[0].name
    return f"group_{group[0].shot}_{make_key([(clip.name, clip.xfade, clip.overlap) for clip in group])}.mp4"


def fingerprint(ctx: StepContext) -> str:
    settings, clips, _ = build_plan(ctx)
    audio = read_voice(ctx)
    sound = file_hash(ctx.path(audio.track, "audio")) if audio else ""
    return make_key([group_name(group) for group in groups_of(clips)], ctx.params.get("transition_seconds", 0.6), settings.fps, sound)


def done(ctx: StepContext) -> bool:
    """Готово, если ролик собран из нынешних картинок и с нынешними настройками."""
    state = ctx.path(STATE)
    if ctx.redo_lines() or not ctx.path(OUTPUT).exists() or not state.exists() or not ctx.path(generate.OUTPUT, "visual").exists():
        return False
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("fingerprint") == fingerprint(ctx)
    except PipelineError:
        return False


# --- ffmpeg ---


def ffmpeg(ctx: StepContext, args: list[str], output: Path) -> None:
    """Результат появляется целиком или никак: при Ctrl+C останется только .part."""
    temp = output.with_name(output.name + ".part")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args, "-f", "mp4", temp.name]
    result = subprocess.run(command, cwd=output.parent, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        lines = result.stderr.strip().splitlines()
        raise PipelineError(f"[{ctx.module}.{ctx.step}] ffmpeg не собрал {output.name}: {' / '.join(lines[-3:]) or 'код ' + str(result.returncode)}")
    temp.replace(output)


def encoder(settings: Settings, frames: int) -> list[str]:
    return [
        "-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
        "-pix_fmt", "yuv420p", "-r", str(settings.fps), "-video_track_timescale", str(settings.fps * 512),
    ]


def camera_filter(camera: str, frames: int, zoom: float) -> str:
    """Движение камеры по картинке. perspective считает положение с долями пикселя, поэтому нет дрожания,
    как у zoompan: тот округляет положение до целых пикселей и картинка дёргается туда-сюда."""
    progress = f"(on/{max(1, frames - 1)})"
    size = f"{zoom}"  # во сколько раз камера приближает картинку на этом кадре
    across, down = "0.5", "0.5"  # где окно камеры в свободном месте: 0 — у левого (верхнего) края, 1 — у правого (нижнего)
    if camera == "zoom_in":
        size = f"(1+{zoom - 1}*{progress})"
    elif camera == "zoom_out":
        size = f"(1+{zoom - 1}*(1-{progress}))"
    elif camera == "pan_right":
        across = progress
    elif camera == "pan_left":
        across = f"(1-{progress})"
    elif camera == "pan_down":
        down = progress
    elif camera == "pan_up":
        down = f"(1-{progress})"
    left, top = f"((W-W/{size})*{across})", f"((H-H/{size})*{down})"
    right, bottom = f"({left}+W/{size})", f"({top}+H/{size})"
    return (
        f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}':x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}':"
        "interpolation=cubic:eval=frame"
    )


def render_clip(ctx: StepContext, clip: Clip, settings: Settings, folder: Path) -> None:
    width, height = settings.width, settings.height
    args: list[str] = []
    chains, labels = [], []
    if clip.is_video:
        args += ["-i", str(clip.sources[0])]
        chains.append(
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1,"
            f"fps={settings.fps},tpad=stop_mode=clone:stop=-1,trim=end_frame={clip.frames},setpts=PTS-STARTPTS,format=yuv420p[v0]"
        )
        labels.append("[v0]")
    else:
        count = len(clip.sources)
        for index, source in enumerate(clip.sources):
            frames = round(clip.frames * (index + 1) / count) - round(clip.frames * index / count)
            args += ["-i", str(source)]
            # Картинка увеличивается один раз и повторяется нужное число кадров, движение считается на каждом.
            camera = "" if clip.camera == "static" else f"{camera_filter(clip.camera, frames, settings.camera_zoom)},"
            chains.append(
                f"[{index}:v]scale={width * SCALE}:{height * SCALE}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width * SCALE}:{height * SCALE},setsar=1,format=yuv420p,loop=loop={frames - 1}:size=1:start=0,"
                f"settb=1/{settings.fps},setpts=N,{camera}scale={width}:{height}:flags=lanczos[v{index}]"
            )
            labels.append(f"[v{index}]")
    last = labels[0]
    if len(labels) > 1:
        chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=1[joined]")
        last = "[joined]"
    ffmpeg(ctx, [*args, "-filter_complex", ";".join(chains), "-map", last, *encoder(settings, clip.frames)], folder / clip.name)


def render_group(ctx: StepContext, group: list[Clip], settings: Settings, folder: Path) -> None:
    """Кадры группы соединяются переходами. У каждого кадра есть запас, поэтому общая длина не меняется."""
    args: list[str] = []
    for clip in group:
        args += ["-threads", "1", "-i", clip.name]
    chains, last, length = [], "[0:v]", group[0].frames
    for index, clip in enumerate(group[1:], 1):
        offset = (length - clip.overlap) / settings.fps
        chains.append(f"{last}[{index}:v]xfade=transition={clip.xfade}:duration={clip.overlap / settings.fps}:offset={offset}[x{index}]")
        last = f"[x{index}]"
        length += clip.frames - clip.overlap
    ffmpeg(ctx, [*args, "-filter_complex", ";".join(chains), "-map", last, *encoder(settings, length)], folder / group_name(group))


def run(ctx: StepContext) -> None:
    if not shutil.which("ffmpeg"):
        raise PipelineError(f"[{ctx.module}.{ctx.step}] не найден ffmpeg — установите его и добавьте в PATH: https://ffmpeg.org/download.html")
    apply_redo(ctx)
    settings, clips, complete = build_plan(ctx)

    folder = ctx.path(CLIPS)
    folder.mkdir(parents=True, exist_ok=True)
    groups = groups_of(clips)
    workers = ctx.params.get("workers", 4)
    audio = read_voice(ctx)
    sound = "со звуком" if audio else "без звука — модуль audio выключен"
    log.info("[%s.%s] кадров: %d, %dx%d, %d к/с; %s", ctx.module, ctx.step, len(clips), settings.width, settings.height, settings.fps, sound)
    if not complete:
        log.warning("[%s.%s] пробная сборка: картинки есть не для всех кадров. Чтобы сделать все, поставьте limit: 0 в 03_visual", ctx.module, ctx.step)

    todo = [clip for clip in clips if not (folder / clip.name).exists()]
    if len(todo) < len(clips):
        log.info("[%s.%s] уже готово кадров: %d, осталось: %d", ctx.module, ctx.step, len(clips) - len(todo), len(todo))
    finished = [0]

    def work(clip: Clip) -> None:
        render_clip(ctx, clip, settings, folder)
        finished[0] += 1
        log.info("[%s.%s] %d/%d: кадр %d", ctx.module, ctx.step, finished[0], len(todo), clip.shot)

    generate.run_parallel(ctx, todo, work, workers)
    joined = [group for group in groups if len(group) > 1 and not (folder / group_name(group)).exists()]
    if joined:
        log.info("[%s.%s] переходы между кадрами: групп %d", ctx.module, ctx.step, len(joined))
        generate.run_parallel(ctx, joined, lambda group: render_group(ctx, group, settings, folder), workers)

    names = [group_name(group) for group in groups]
    playlist = folder / "playlist.txt"
    playlist.write_text("".join(f"file '{name}'\n" for name in names), encoding="utf-8")
    args = ["-f", "concat", "-safe", "0", "-i", playlist.name]
    if audio:
        seconds = sum(clip.frames - clip.overlap for clip in clips) / settings.fps  # при пробной сборке дорожка может быть длиннее
        args += ["-i", str(ctx.path(audio.track, "audio")), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-t", str(seconds)]
    ffmpeg(ctx, [*args, "-c:v", "copy", "-movflags", "+faststart"], folder / "video.mp4")
    (folder / "video.mp4").replace(ctx.path(OUTPUT))

    # Куски от прежних версий картинок и настроек больше не нужны.
    keep = {*names, *(clip.name for clip in clips), playlist.name}
    for path in folder.iterdir():
        if path.name not in keep:
            path.unlink()
    ctx.write(STATE, json.dumps({"fingerprint": fingerprint(ctx), "complete": complete}, indent=2))
    log.info("[%s.%s] ролик: %s", ctx.module, ctx.step, ctx.path(OUTPUT))
