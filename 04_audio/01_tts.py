"""Озвучка: текст диктора из 02_storyboard/storyboard.json → 04_audio/1.wav, 2.wav, ..., общая дорожка voice.wav и audio.json.

Каждый кадр озвучивается отдельно, поэтому точно известно, когда он начинается и заканчивается: монтаж берёт
время кадров из audio.json, и картинка совпадает с голосом. После кадра, где заканчивается предложение, — пауза
pause_seconds, между блоками сценария — block_pause_seconds.

Если картинки сделаны пробно (limit в 03_visual), озвучиваются только их кадры — чтобы не платить за лишнее.
Готовый файл второй раз не генерируется. Переделать кадр: удалите его файл (например, 5.wav) и запустите программу.
Если изменился текст кадра или настройки генератора в config.yaml, кадр переозвучится сам, а старый файл уйдёт в папку old.
Правку шаблона в кабинете Lumean программа не заметит: чтобы озвучить новым голосом, создайте новый шаблон и впишите его id.
Переделать через 04_audio/redo.txt: «5» — озвучить кадр заново, «5: новый текст» — заменить текст кадра в раскадровке и озвучить.
"""

import hashlib
import io
import json
import logging
import shutil
import subprocess
import wave

from pydantic import BaseModel

from core.pipeline import load_step
from core.step import REDO, PipelineError, StepContext

generate = load_step("visual", "generate")

OUTPUT = "audio.json"
TRACK = "voice.wav"  # вся озвучка одной дорожкой
SHOW = TRACK
REDO_HELP = """# Какие кадры озвучить заново. Одна строка — один кадр:
#   5                     — озвучить ещё раз тем же текстом
#   5: новый текст кадра  — заменить текст в раскадровке и озвучить его
# Строки с # в начале не считаются. Старые варианты уйдут в папку old.
"""
KEYS = "keys.json"  # с каким текстом и голосом сделан файл каждого кадра
RATE = 48000
EXTENSIONS = (".wav", ".mp3", ".ogg")

log = logging.getLogger(__name__)


class ShotAudio(BaseModel):
    id: int
    file: str
    start: float
    end: float  # вместе с паузой после кадра


class AudioSet(BaseModel):
    key: str  # из чего собрана дорожка — чтобы заметить новый текст, голос или паузы
    track: str
    duration: float
    shots: list[ShotAudio]


def wanted_shots(ctx: StepContext, board) -> list:
    """Все кадры, а после пробного запуска картинок — только те, для которых картинки есть."""
    images = ctx.read_json(generate.OUTPUT, generate.ImageSet, module="visual")
    if images is None or images.complete:
        return board.shots
    ready = {shot.id for shot in images.shots}
    return [shot for shot in board.shots if shot.id in ready]


def shot_keys(ctx: StepContext, shots: list) -> dict[str, str]:
    provider = ctx.media("audio")
    voice = json.dumps([provider.name, provider.config], sort_keys=True, ensure_ascii=False)
    return {str(shot.id): hashlib.sha1(f"{voice}\n{shot.text}".encode("utf-8")).hexdigest() for shot in shots}


def set_key(ctx: StepContext, keys: dict[str, str]) -> str:
    params = ctx.params
    parts = [keys, params.get("pause_seconds", 0.3), params.get("block_pause_seconds", 0.7)]
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def done(ctx: StepContext) -> bool:
    """Готово, если озвучены все нужные кадры нынешним текстом и голосом и все файлы на месте."""
    audio = ctx.read_json(OUTPUT, AudioSet)
    if audio is None or not ctx.path(TRACK).exists() or ctx.redo_lines():
        return False
    keys = shot_keys(ctx, wanted_shots(ctx, generate.read_storyboard(ctx)))
    return audio.key == set_key(ctx, keys) and all(ctx.path(shot.file).exists() for shot in audio.shots)


def retire_changed(ctx: StepContext, keys: dict[str, str]) -> None:
    """Файлы кадров с другим текстом или голосом — в old/. Новые ключи записываются до генерации,
    поэтому оборванный запуск не примет старый файл за готовый."""
    path = ctx.path(KEYS)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for base, key in keys.items():
        for extension in EXTENSIONS:
            file = ctx.path(base + extension)
            if file.exists() and old.get(base) != key:
                ctx.keep_old(file)
    ctx.write(KEYS, json.dumps({**old, **keys}, indent=2))


def apply_redo(ctx: StepContext, board) -> None:
    """redo.txt: новый текст записывается в раскадровку (кадр переозвучится сам, см. retire_changed),
    кадр без текста просто уходит в old/."""
    items = ctx.redo_items()
    if not items:
        return
    shots = {str(shot.id): shot for shot in board.shots}
    unknown = [name for name in items if name not in shots]
    if unknown:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] в {REDO} нет таких кадров: {', '.join(unknown)}. Пишите номер кадра — например, 5")
    for name, text in items.items():
        if text:
            shots[name].text = text
        else:
            for extension in EXTENSIONS:
                if ctx.path(name + extension).exists():
                    ctx.keep_old(ctx.path(name + extension))
    if any(items.values()):
        generate.storyboard.save(ctx, board)
    ctx.clear_redo(REDO_HELP)


def decode(ctx: StepContext, name: str) -> bytes:
    """Файл → отсчёты звука. Длительность считается по ним: заголовок файла от генератора может её не знать."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(ctx.path(name)), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        error = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise PipelineError(f"[{ctx.module}.{ctx.step}] не удалось прочитать {name}: {' '.join(error)}. Удалите файл и запустите ещё раз")
    return result.stdout


def run(ctx: StepContext) -> None:
    if not shutil.which("ffmpeg"):
        raise PipelineError(f"[{ctx.module}.{ctx.step}] не найден ffmpeg — установите его и добавьте в PATH: https://ffmpeg.org/download.html")
    board = generate.read_storyboard(ctx)
    apply_redo(ctx, board)
    shots = wanted_shots(ctx, board)
    if len(shots) < len(board.shots):
        log.warning("[%s.%s] пробная озвучка: кадров %d из %d — как и картинок", ctx.module, ctx.step, len(shots), len(board.shots))
    provider = ctx.media("audio")
    log.info("[%s.%s] кадров: %d, генератор: %s (%s)", ctx.module, ctx.step, len(shots), provider.name, provider.model)

    keys = shot_keys(ctx, shots)
    retire_changed(ctx, keys)
    jobs = {str(shot.id): (lambda text=shot.text: provider.speak(text)) for shot in shots}
    files = generate.produce(ctx, jobs, provider, EXTENSIONS)

    params = ctx.params
    pause, block_pause = params.get("pause_seconds", 0.3), params.get("block_pause_seconds", 0.7)
    track = io.BytesIO()
    samples = 0
    records = []
    with wave.open(track, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        for index, shot in enumerate(shots):
            speech = decode(ctx, files[str(shot.id)])
            following = shots[index + 1] if index + 1 < len(shots) else None
            gap = block_pause if following is None or following.block != shot.block else pause if shot.text.rstrip()[-1:] in ".!?…" else 0
            silence = round(gap * RATE)
            out.writeframes(speech + b"\0\0" * silence)
            start = samples / RATE
            samples += len(speech) // 2 + silence
            records.append(ShotAudio(id=shot.id, file=files[str(shot.id)], start=round(start, 3), end=round(samples / RATE, 3)))
    ctx.write_bytes(TRACK, track.getvalue())
    ctx.write_json(OUTPUT, AudioSet(key=set_key(ctx, keys), track=TRACK, duration=round(samples / RATE, 3), shots=records))
    log.info("[%s.%s] озвучка: %d:%04.1f — %s", ctx.module, ctx.step, samples // RATE // 60, samples / RATE % 60, ctx.path(TRACK))
