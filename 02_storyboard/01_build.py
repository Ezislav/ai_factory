"""Раскадровка: 01_scenario/script.md → 02_storyboard/storyboard.json (и storyboard.md — чтобы читать глазами).

Текст диктора режется на короткие фразы и нумеруется. Нейросеть выбирает единый визуальный стиль
ролика (style — его не нужно задавать в config.yaml), группирует фразы в кадры и для каждого
придумывает, что на экране (по-русски и промпт для генератора по-английски), план, движение
камеры, переход и будет ли кадр видео (media: video — не больше video_shots на ролик).
В кадре может быть несколько картинок (images_per_shot) — они сменяют друг друга через равные промежутки,
но каждая держится не меньше image_seconds_min секунд: в коротком кадре лишние отбрасываются. Начало, конец и
длительность кадров считает код по числу слов и темпу диктора (words_per_minute из scenario) — это оценка:
итоговое время кадров монтаж берёт из настоящей озвучки (04_audio).

Правки автора из 02_storyboard/redo.txt меняют только те кадры, которых касаются: их текст и время не трогаются,
у остальных кадров останутся готовые картинки. Прежний вариант раскадровки уходит в 02_storyboard/old/.
"""

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.pipeline import load_step
from core.step import PipelineError, StepContext

SCRIPT = load_step("scenario", "revise").OUTPUT

OUTPUT = "storyboard.json"
PREVIEW = "storyboard.md"
SHOW = PREVIEW
REQUIRED = True  # storyboard.json — выход блока, его читают изображения, озвучка и монтаж
REDO_PROMPT = "01_redo.md"
REDO_HELP = """# Правки к раскадровке (storyboard.md). Пишите обычным языком, одна строка — одна правка:
#   5: покажи закат над морем, план общий
#   12: вместо наезда панорама вправо
#   Сделай стиль всего ролика темнее
# Текст диктора и границы кадров здесь не меняются: текст правится на этапе 1 или 4.
# Строки с # в начале не считаются. Прежний вариант уйдёт в папку old.
"""

ShotType = Literal["wide", "medium", "close", "macro", "aerial"]
Camera = Literal["static", "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"]
Transition = Literal["cut", "crossfade", "fade_black"]
MediaKind = Literal["image", "video"]

log = logging.getLogger(__name__)

SHOT_LABELS = {"wide": "общий", "medium": "средний", "close": "крупный", "macro": "макро", "aerial": "с высоты"}
CAMERA_LABELS = {
    "static": "статично",
    "zoom_in": "наезд",
    "zoom_out": "отъезд",
    "pan_left": "панорама влево",
    "pan_right": "панорама вправо",
    "pan_up": "панорама вверх",
    "pan_down": "панорама вниз",
}
TRANSITION_LABELS = {"cut": "склейка", "crossfade": "наплыв", "fade_black": "через чёрное"}


# --- Ответ нейросети: какие фразы в каком кадре и как кадр выглядит ---


class ShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: int = Field(description="Номер первой фразы кадра")
    last: int = Field(description="Номер последней фразы кадра (равен first, если фраза одна)")
    visual: str = Field(description="Что на экране: сюжет, объекты, фон, свет, ракурс — одно-два предложения")
    image_prompts: list[str] = Field(description="Картинки кадра по порядку показа — подробные промпты на английском")
    media: MediaKind = Field(description="image — картинка с движением камеры, video — короткое видео из картинки")
    motion: str = Field(description="Для video: что движется в кадре, на английском; для image — пустая строка")
    shot_type: ShotType = Field(description="План")
    camera: Camera = Field(description="Движение камеры по картинке")
    transition: Transition = Field(description="Переход к этому кадру от предыдущего")


class StoryboardPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = Field(description="Единый визуальный стиль всего ролика для генератора картинок, на английском")
    shots: list[ShotPlan]


class ShotEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(description="Номер кадра")
    visual: str = Field(description="Что на экране: сюжет, объекты, фон, свет, ракурс — одно-два предложения")
    image_prompts: list[str] = Field(description="Картинки кадра по порядку показа — подробные промпты на английском; столько же, сколько было")
    media: MediaKind = Field(description="image — картинка с движением камеры, video — короткое видео из картинки")
    motion: str = Field(description="Для video: что движется в кадре, на английском; для image — пустая строка")
    shot_type: ShotType = Field(description="План")
    camera: Camera = Field(description="Движение камеры по картинке")
    transition: Transition = Field(description="Переход к этому кадру от предыдущего")


class StoryboardEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = Field(description="Стиль ролика на английском; если правки его не касаются — прежний слово в слово")
    shots: list[ShotEdit] = Field(description="Только изменившиеся кадры")


# --- Итоговая раскадровка: то, что читают следующие модули ---


class Shot(BaseModel):
    id: int
    block: str = Field(description="Блок сценария «## …», к которому относится кадр")
    start: float = Field(description="Начало, секунды от начала ролика")
    end: float = Field(description="Конец, секунды")
    duration: float = Field(description="Длительность, секунды")
    text: str = Field(description="Текст диктора в этом кадре")
    visual: str
    image_prompts: list[str] = Field(default_factory=list, description="Картинки кадра; сменяют друг друга через равные промежутки")
    media: MediaKind = "image"
    motion: str = ""
    shot_type: ShotType
    camera: Camera
    transition: Transition

    @model_validator(mode="before")
    @classmethod
    def _single_prompt(cls, data):
        """Раскадровки, сделанные до images_per_shot, хранят один промпт в image_prompt."""
        if isinstance(data, dict) and "image_prompt" in data:
            data = dict(data)
            prompt = data.pop("image_prompt")
            data.setdefault("image_prompts", [prompt] if prompt else [])
        return data


class Storyboard(BaseModel):
    title: str
    style: str = Field(description="Общий визуальный стиль картинок")
    aspect_ratio: str
    words_per_minute: int
    duration: float = Field(description="Длительность ролика, секунды")
    shots: list[Shot]

    def to_markdown(self) -> str:
        lines = [
            f"# Раскадровка: {self.title}",
            "",
            f"{len(self.shots)} кадров, {format_time(self.duration)}. Формат {self.aspect_ratio}. Стиль: {self.style}",
            "",
            "Время посчитано по темпу диктора "
            f"({self.words_per_minute} слов в минуту) и уточнится после озвучки.",
        ]
        block = None
        for shot in self.shots:
            if shot.block != block:
                block = shot.block
                lines += ["", f"## {block}", "", "| № | Время | Текст диктора | Что на экране | План, камера | Переход |", "|---|---|---|---|---|---|"]
            visual = shot.visual + (f"<br>**Видео:** {shot.motion}" if shot.media == "video" else "")
            if len(shot.image_prompts) > 1:
                visual += f"<br>**Картинок:** {len(shot.image_prompts)}, по {shot.duration / len(shot.image_prompts):.1f} с"
            camera = f"{SHOT_LABELS[shot.shot_type]}, {CAMERA_LABELS[shot.camera]}"
            time = f"{format_time(shot.start)}–{format_time(shot.end)}<br>{shot.duration:.1f} с"
            cells = [str(shot.id), time, shot.text, visual, camera, TRANSITION_LABELS[shot.transition]]
            lines.append("| " + " | ".join(cell.replace("|", "/") for cell in cells) + " |")
        return "\n".join(lines) + "\n"


def format_time(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


# --- Сценарий → пронумерованные фразы ---


@dataclass
class Phrase:
    block: str
    text: str
    seconds: float


def split_phrases(sentence: str, max_words: int) -> list[str]:
    """Длинное предложение режется по запятым и тире на куски не длиннее max_words слов (если получится)."""
    if len(sentence.split()) <= max_words:
        return [sentence]
    parts = re.split(r"(?<=[,;:])\s+|\s+(?=[—–]\s)", sentence)
    chunks, current = [], ""
    for part in parts:
        candidate = f"{current} {part}".strip()
        if current and len(candidate.split()) > max_words:
            chunks.append(current)
            current = part
        else:
            current = candidate
    return chunks + [current] if current else chunks


def parse_script(text: str, words_per_minute: int, max_words: int) -> tuple[str, list[Phrase]]:
    """«# Название», блоки «## …» и абзацы текста диктора. Строки «> …» (старые пометки кадров) пропускаются."""
    title, block, phrases, paragraph = "", "", [], []

    def flush() -> None:
        sentences = re.split(r"(?<=[.!?…])\s+", " ".join(paragraph))
        for sentence in sentences:
            for piece in split_phrases(sentence.strip(), max_words):
                if piece:
                    phrases.append(Phrase(block, piece, len(piece.split()) * 60 / words_per_minute))
        paragraph.clear()

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            flush()
            block = line[3:].strip()
        elif line.startswith("# "):
            flush()
            title = line[2:].strip()
        elif not line or line.startswith(">"):
            flush()
        else:
            paragraph.append(line)
    flush()
    return title, phrases


def numbered_phrases(phrases: list[Phrase]) -> str:
    lines, block = [], None
    for number, phrase in enumerate(phrases, 1):
        if phrase.block != block:
            block = phrase.block
            lines += ["", f"## {block}"]
        lines.append(f"[{number}] ({phrase.seconds:.1f} с) {phrase.text}")
    return "\n".join(lines).strip()


def plan_checker(phrases: list[Phrase], max_seconds: float, video_shots: int, video_seconds_max: float, images_per_shot: int):
    """Проверка ответа: все фразы по порядку, без пропусков и повторов, кадр — внутри одного блока, лимит видео,
    число картинок в кадре."""

    def check(plan: StoryboardPlan) -> None:
        problems, expected = [], 1
        videos = sum(shot.media == "video" for shot in plan.shots)
        if videos > video_shots:
            problems.append(f"видео-кадров {videos}, а можно не больше {video_shots} — остальные сделай image")
        for index, shot in enumerate(plan.shots, 1):
            images = 1 if shot.media == "video" else images_per_shot
            if len([prompt for prompt in shot.image_prompts if prompt.strip()]) != images:
                kind = "видео-кадре" if shot.media == "video" else "кадре"
                problems.append(f"в {kind} {index} промптов картинок {len(shot.image_prompts)}, а нужно ровно {images}")
            if shot.first != expected:
                problems.append(f"кадр {index} начинается с фразы {shot.first}, а должен с {expected}")
            if not shot.first <= shot.last <= len(phrases):
                problems.append(f"кадр {index}: неверный диапазон фраз {shot.first}–{shot.last}")
            else:
                covered = phrases[shot.first - 1 : shot.last]
                if len({phrase.block for phrase in covered}) > 1:
                    problems.append(f"кадр {index} (фразы {shot.first}–{shot.last}) захватывает два блока — раздели его")
                seconds = sum(phrase.seconds for phrase in covered)
                if len(covered) > 1 and seconds > max_seconds * 1.5:
                    problems.append(f"кадр {index} (фразы {shot.first}–{shot.last}) длится {seconds:.0f} с — раздели его")
                if shot.media == "video" and seconds > video_seconds_max:
                    problems.append(
                        f"видео-кадр {index} длится {seconds:.0f} с, а видео бывает не длиннее {video_seconds_max:g} с — раздели его или сделай image"
                    )
            expected = max(expected, shot.last + 1)
        if expected <= len(phrases):
            problems.append(f"фразы {expected}–{len(phrases)} не попали ни в один кадр")
        if problems:
            more = f" и ещё {len(problems) - 8}" if len(problems) > 8 else ""
            raise ValueError("; ".join(problems[:8]) + more + ". Кадры идут подряд и покрывают все фразы ровно по одному разу")

    return check


def save(ctx: StepContext, storyboard: Storyboard) -> None:
    ctx.write_json(OUTPUT, storyboard, module="storyboard")
    ctx.write(PREVIEW, storyboard.to_markdown(), module="storyboard")


def done(ctx: StepContext) -> bool:
    return ctx.path(OUTPUT).exists() and not ctx.redo_lines()


def apply_redo(ctx: StepContext, wishes: list[str]) -> None:
    """Правки автора → изменившиеся кадры. Текст и время кадров остаются прежними."""
    board = ctx.read_json(OUTPUT, Storyboard)
    shots = {shot.id: shot for shot in board.shots}
    video_shots = ctx.params.get("video_shots", 0)

    def check(edit: StoryboardEdit) -> None:
        problems = []
        for item in edit.shots:
            if item.id not in shots:
                problems.append(f"кадра {item.id} нет")
            elif len(item.image_prompts) != max(1, len(shots[item.id].image_prompts)):
                problems.append(f"в кадре {item.id} должно остаться промптов картинок: {max(1, len(shots[item.id].image_prompts))}")
        media = {**{shot.id: shot.media for shot in board.shots}, **{item.id: item.media for item in edit.shots}}
        if sum(kind == "video" for kind in media.values()) > max(video_shots, sum(shot.media == "video" for shot in board.shots)):
            problems.append(f"видео-кадров можно не больше {video_shots}")
        if problems:
            raise ValueError("; ".join(problems))

    system, user = ctx.render_prompt(template=REDO_PROMPT, wishes=wishes, style=board.style, shots=[shot.model_dump() for shot in board.shots])
    edit = ctx.ask_json(system, user, StoryboardEdit, check=check)
    updates = {item.id: item.model_dump(exclude={"id"}) for item in edit.shots}
    changed = board.model_copy(update={
        "style": edit.style.strip() or board.style,
        "shots": [shot.model_copy(update=updates.get(shot.id, {})) for shot in board.shots],
    })
    for name in (OUTPUT, PREVIEW):
        if ctx.path(name).exists():
            ctx.keep_old(ctx.path(name))
    save(ctx, changed)
    ctx.clear_redo(REDO_HELP)
    log.info("[%s.%s] правки внесены, изменилось кадров: %d", ctx.module, ctx.step, len(updates))


def run(ctx: StepContext) -> None:
    wishes = ctx.redo_lines()
    board, script = ctx.path(OUTPUT), ctx.path(SCRIPT, "scenario")
    # С правками раскадровка заново не строится — если только сценарий не новее её.
    if not wishes or not board.exists() or (script.exists() and script.stat().st_mtime_ns > board.stat().st_mtime_ns):
        build(ctx)
    if wishes:
        apply_redo(ctx, wishes)


def build(ctx: StepContext) -> None:
    params = ctx.params
    words_per_minute = ctx.params_of("scenario").get("words_per_minute", 140)
    title, phrases = parse_script(ctx.read(SCRIPT, module="scenario"), words_per_minute, params.get("max_phrase_words", 16))
    if not phrases:
        raise PipelineError(f"[storyboard.build] в {SCRIPT} нет текста диктора")

    video_shots, video_seconds_max = params.get("video_shots", 0), params.get("video_seconds_max", 8)
    images_per_shot, image_seconds_min = max(1, params.get("images_per_shot", 1)), params.get("image_seconds_min", 2)
    system, user = ctx.render_prompt(
        title=title or ctx.topic,
        phrases=numbered_phrases(phrases),
        video_shots=video_shots,
        video_seconds_max=video_seconds_max,
        images_per_shot=images_per_shot,
    )
    check = plan_checker(phrases, params.get("shot_seconds_max", 8), video_shots, video_seconds_max, images_per_shot)
    plan = ctx.ask_json(system, user, StoryboardPlan, check=check)

    shots, start = [], 0.0
    for number, item in enumerate(plan.shots, 1):
        covered = phrases[item.first - 1 : item.last]
        duration = sum(phrase.seconds for phrase in covered)
        # Короткий кадр: оставляем столько первых картинок, чтобы каждая держалась не меньше image_seconds_min.
        fits = max(1, int(duration // image_seconds_min)) if image_seconds_min > 0 else len(item.image_prompts)
        item.image_prompts = item.image_prompts[:fits]
        shots.append(
            Shot(
                id=number,
                block=covered[0].block,
                start=round(start, 2),
                end=round(start + duration, 2),
                duration=round(duration, 2),
                text=" ".join(phrase.text for phrase in covered),
                **item.model_dump(exclude={"first", "last"}),
            )
        )
        start += duration

    storyboard = Storyboard(
        title=title or ctx.topic,
        style=plan.style,
        aspect_ratio=params.get("aspect_ratio", "16:9"),
        words_per_minute=words_per_minute,
        duration=round(start, 2),
        shots=shots,
    )
    save(ctx, storyboard)
