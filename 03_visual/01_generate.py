"""Картинки для кадров раскадровки → 03_visual/1.png, 2.png, ... и список images.json.

Сколько картинок в кадре, решает раскадровка (images_per_shot). Одна картинка — файл по номеру кадра (5.png),
несколько — с номером картинки (5_1.png, 5_2.png): они сменяют друг друга внутри кадра.
Картинка делается и для кадров с media: video — она станет первым кадром видео (шаг 03_animate).
Готовая картинка не генерируется второй раз, если описание её сцены не поменялось: файл берётся как есть.
Так оборванный запуск продолжается с места остановки, а деньги не тратятся повторно. Если после новой
раскадровки описание стало другим, картинка переделается сама, а старый файл уйдёт в 03_visual/old/.

Переделать картинку: впишите её имя в 03_visual/redo.txt (можно с пожеланием: «5: сделай закат ярче»)
и запустите программу снова. Старый вариант уйдёт в 03_visual/old/, новое описание сцены запомнится
в fixes.json. Проще всего — после проверки (шаг 02_review): она сама предложит, что переделать.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from core.llm import Image
from core.media import Media, MediaError, MediaProvider
from core.media.base import mime_type
from core.pipeline import load_step
from core.step import OLD, REDO, PipelineError, StepContext

storyboard = load_step("storyboard", "build")

OUTPUT = "images.json"
REQUIRED = True  # images.json — выход блока, его читают видео и монтаж
IMAGE_EXTENSIONS = (".png", ".jpg", ".webp")
FIXES = "fixes.json"  # новые описания сцен для переделанных картинок
REVIEW = "review.json"  # отчёт проверки (шаг 02_review) — из него берутся советы для переделки
REDO_PROMPT = "01_redo.md"
REDO_HELP = """# Какие картинки переделать. Одна строка — одна картинка: её имя без .png, после двоеточия — пожелание.
#   5                         — переделать по совету проверки (или просто ещё раз, если замечаний нет)
#   12_2: сделай закат ярче   — переделать с вашим пожеланием
#   7                         — если в кадре несколько картинок (7_1, 7_2), переделаются все
# Строки с # в начале не считаются. Старые варианты уйдут в папку old.
"""

FRAMING = {
    "wide": "wide shot",
    "medium": "medium shot",
    "close": "close-up",
    "macro": "extreme close-up, macro photography",
    "aerial": "aerial view from above",
}

log = logging.getLogger(__name__)


class ShotImage(BaseModel):
    id: int
    media: Literal["image", "video"]
    files: list[str]  # по порядку показа; делят кадр поровну
    descriptions: list[str] = []  # описания сцен (без стиля) — по ним проверка советует исправления
    prompts: list[str]  # полные промпты генератора


class ImageSet(BaseModel):
    aspect_ratio: str
    complete: bool = True  # false — пробный запуск (limit), картинки есть не для всех кадров
    style: str = ""  # стиль раскадровки, с которым сделаны картинки
    shots: list[ShotImage]


def read_storyboard(ctx: StepContext):
    board = ctx.read_json(storyboard.OUTPUT, storyboard.Storyboard, module="storyboard")
    if board is None:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет раскадровки {storyboard.OUTPUT} — её делает модуль storyboard")
    return board


def file_names(shot) -> list[str]:
    """Имена файлов без расширения: 5 — одна картинка в кадре 5; 5_1, 5_2 — несколько."""
    count = max(1, len(shot.image_prompts))
    return [str(shot.id)] if count == 1 else [f"{shot.id}_{index}" for index in range(1, count + 1)]


def natural(base: str) -> tuple[int, ...]:
    """Порядок 2 < 10 < 10_2, а не по алфавиту."""
    return tuple(int(part) for part in base.split("_"))


def run_parallel(ctx: StepContext, items: list, fn: Callable, workers: int, stop: threading.Event | None = None) -> list:
    """fn(item) для всех items в несколько потоков. Ctrl+C: не начатые отменяются, уже отправленные
    запросы завершаются и их результат сохраняется (он уже оплачен)."""
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [pool.submit(fn, item) for item in items]
    try:
        while not all(future.done() for future in futures):
            wait(futures, timeout=0.2)  # короткими шагами — чтобы Ctrl+C срабатывал сразу
    except KeyboardInterrupt:
        if stop:
            stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        running = sum(future.running() for future in futures)
        if running:
            log.warning("[%s.%s] остановка: дожидаюсь уже отправленных запросов (%d), их результат сохранится. "
                        "Ctrl+C ещё раз — прервать сразу", ctx.module, ctx.step, running)
        raise
    pool.shutdown()
    return [future.result() for future in futures]


def produce(
    ctx: StepContext,
    jobs: dict[str, Callable[[], Media]],
    provider: MediaProvider,
    extensions: tuple[str, ...],
) -> dict[str, str]:
    """Выполнить jobs (имя файла без расширения → генерация) в несколько потоков, с повторами.
    Вернуть имя без расширения → имя файла.

    Если файл уже есть, он не генерируется заново. Если часть кадров не получилась, остальные
    уже сохранены, а ошибка перечисляет неудачные — следующий запуск сделает только их.
    """
    params = ctx.params
    attempts, workers = params.get("attempts", 3), max(1, params.get("workers", 4))
    lock, stop = threading.Lock(), threading.Event()
    files: dict[str, str] = {}
    failed: dict[str, str] = {}

    def existing(base: str) -> str | None:
        return next((base + ext for ext in extensions if ctx.path(base + ext).exists()), None)

    todo = []
    for base in jobs:
        name = existing(base)
        if name:
            files[base] = name
        else:
            todo.append(base)
    reused = len(files)
    if reused:
        log.info("[%s.%s] уже есть: %d (не генерируются заново), осталось: %d", ctx.module, ctx.step, reused, len(todo))

    def work(base: str) -> None:
        if stop.is_set():
            return
        for attempt in range(1, attempts + 1):
            try:
                media = jobs[base]()
                break
            except MediaError as e:
                if e.fatal:
                    stop.set()
                if not e.retry or attempt == attempts:
                    with lock:
                        failed[base] = str(e)
                    return
                log.warning("[%s.%s] %s: %s — повтор через %d с", ctx.module, ctx.step, base, e, 10 * attempt)
                time.sleep(10 * attempt)
        name = base + media.extension
        for ext in extensions:  # старый файл с другим расширением (png → jpg) не должен остаться
            if ext != media.extension:
                ctx.path(base + ext).unlink(missing_ok=True)
        ctx.write_bytes(name, media.data)
        with lock:
            files[base] = name
            ctx.log_media(provider, media, file=name)
            log.info("[%s.%s] %d/%d: %s", ctx.module, ctx.step, len(files), len(jobs), name)

    run_parallel(ctx, todo, work, workers, stop)

    if failed:
        details = "; ".join(f"{base}: {error}" for base, error in sorted(failed.items(), key=lambda item: natural(item[0]))[:5])
        not_started = len(todo) - len(failed) - (len(files) - reused)
        rest = f", ещё {not_started} не запускались — ошибка не исправится повтором" if not_started else ""
        raise PipelineError(
            f"[{ctx.module}.{ctx.step}] не получилось {len(failed)} из {len(jobs)}{rest}: {details}. "
            "Готовые файлы сохранены — запустите ещё раз, чтобы сделать остальные"
        )
    return files


def run(ctx: StepContext) -> None:
    board = read_storyboard(ctx)
    limit = ctx.params.get("limit", 0)
    trial = 0 < limit < len(board.shots)  # limit не меньше числа кадров — это уже полный прогон
    shots = board.shots[:limit] if trial else board.shots

    descriptions = scene_descriptions(ctx, board)
    redo = read_redo(ctx)
    if redo:
        apply_redo(ctx, board, redo, descriptions)
    retire_changed(ctx, board, descriptions)

    prompts = {
        base: ctx.render_prompt(
            description=descriptions[base],
            framing=FRAMING[shot.shot_type],
            style=board.style,
            aspect_ratio=board.aspect_ratio,
        )[1]
        for shot in shots
        for base in file_names(shot)
    }
    provider = ctx.media("image")
    log.info(
        "[%s.%s] кадров: %d, картинок: %d, генератор: %s (%s)",
        ctx.module, ctx.step, len(shots), len(prompts), provider.name, provider.model,
    )
    jobs = {base: (lambda prompt=prompt: provider.generate(prompt, board.aspect_ratio)) for base, prompt in prompts.items()}
    files = produce(ctx, jobs, provider, IMAGE_EXTENSIONS)

    # Пробный запуск тоже записывает images.json (complete: false) — чтобы проверка могла посмотреть картинки.
    # Следующий запуск увидит complete: false и доделает остальные кадры.
    ctx.write_json(
        OUTPUT,
        ImageSet(
            aspect_ratio=board.aspect_ratio,
            complete=not trial,
            style=board.style,
            shots=[
                ShotImage(
                    id=shot.id,
                    media=shot.media,
                    files=[files[base] for base in file_names(shot)],
                    descriptions=[descriptions[base] for base in file_names(shot)],
                    prompts=[prompts[base] for base in file_names(shot)],
                )
                for shot in shots
            ],
        ),
    )
    if trial and "review" not in ctx.steps and not ctx.settings.step_by_step:  # иначе об этом скажет проверка — после того как посмотрит картинки
        raise PipelineError(trial_message(ctx, len(shots), len(board.shots), len(files)))


def trial_message(ctx: StepContext, done_shots: int, all_shots: int, images: int) -> str:
    return (
        f"Пробный запуск: готово кадров — {done_shots} из {all_shots} (картинок: {images}). Посмотрите их в папке "
        f"{ctx.path('').relative_to(ctx.project.dir)}. Чтобы сделать остальные, поставьте limit: 0 в config.yaml. "
        f"Собрать ролик только из готовых кадров: --only 4,5"
    )


def done(ctx: StepContext) -> bool:
    """Готово, если все картинки на месте и совпадают с раскадровкой, это не пробный запуск и в redo.txt ничего не отмечено."""
    images = ctx.read_json(OUTPUT, ImageSet)
    if images is None or read_redo(ctx):
        return False
    # В пошаговом режиме пробные кадры (limit) — законченный результат этапа: автор их уже смотрит.
    trial = not images.complete and ctx.settings.step_by_step and len(images.shots) == ctx.params.get("limit", 0)
    if not images.complete and not trial:
        return False
    board = read_storyboard(ctx)
    descriptions = scene_descriptions(ctx, board)
    if not trial and set(descriptions) != {Path(name).stem for shot in images.shots for name in shot.files}:
        return False
    if (images.style and images.style != board.style) or any(
        descriptions.get(Path(name).stem) != old for shot in images.shots for name, old in zip(shot.files, shot.descriptions)
    ):
        return False
    return all(ctx.path(name).exists() for shot in images.shots for name in shot.files)


def retire_changed(ctx: StepContext, board, descriptions: dict[str, str]) -> None:
    """Картинки, у которых поменялось описание сцены или стиль ролика, — в old/. Их кадры сразу убираются из images.json,
    поэтому после оборванного запуска уже сделанная новая картинка не примется за старую."""
    images = ctx.read_json(OUTPUT, ImageSet)
    if images is None:
        return
    new_style = images.style and images.style != board.style
    kept = []
    for shot in images.shots:
        changed = [name for name, old in zip(shot.files, shot.descriptions) if new_style or descriptions.get(Path(name).stem) != old]
        for name in changed:
            if ctx.path(name).exists():
                ctx.keep_old(ctx.path(name))
        if not changed:
            kept.append(shot)
    if len(kept) < len(images.shots):
        log.info("[%s.%s] поменялись описания кадров: %d — их картинки переделаются", ctx.module, ctx.step, len(images.shots) - len(kept))
        ctx.write_json(OUTPUT, ImageSet(aspect_ratio=images.aspect_ratio, complete=False, style=board.style, shots=kept))


# --- Переделка: redo.txt → новые описания сцен, старые картинки → old/ ---


def scene_descriptions(ctx: StepContext, board) -> dict[str, str]:
    """Описание сцены для каждой картинки: из раскадровки, с поправками прошлых переделок (fixes.json)."""
    fixes = read_fixes(ctx)
    result = {}
    for shot in board.shots:
        for base, description in zip(file_names(shot), shot.image_prompts or [shot.visual]):
            fix = fixes.get(base)
            # Поправка действует, пока раскадровка не поменялась: после новой раскадровки она уже не к месту.
            result[base] = fix["to"] if fix and fix["from"] == description else description
    return result


def read_fixes(ctx: StepContext) -> dict[str, dict[str, str]]:
    path = ctx.path(FIXES)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_redo(ctx: StepContext) -> dict[str, str]:
    """redo.txt: строки «5», «12_2: пожелание» или «5.png»; # — комментарий. → {имя без расширения: пожелание}."""
    redo = {}
    for name, wish in ctx.redo_items().items():
        for ext in IMAGE_EXTENSIONS:
            name = name.removesuffix(ext)
        redo[name] = wish
    return redo


def write_redo(ctx: StepContext, suggestions: list[tuple[str, str]] = ()) -> None:
    """Шаблон redo.txt: подсказки проверки закомментированы — переделывается только то, что отметил автор."""
    lines = [REDO_HELP]
    if suggestions:
        lines.append("# Проверке не понравились — уберите # в начале строки, чтобы переделать:")
        lines += [f"# {name}:    # {reason}" for name, reason in suggestions]
    ctx.write(REDO, "\n".join(lines))


def apply_redo(ctx: StepContext, board, redo: dict[str, str], descriptions: dict[str, str]) -> None:
    """Новые описания для отмеченных картинок, старые файлы — в old/, redo.txt очищается."""
    shot_files = {str(shot.id): file_names(shot) for shot in board.shots}
    wanted: dict[str, str] = {}
    unknown = []
    for name, wish in redo.items():
        if name in descriptions:
            wanted[name] = wish
        elif name in shot_files:  # номер кадра с несколькими картинками — все его картинки
            wanted.update({base: wish for base in shot_files[name]})
        else:
            unknown.append(name)
    if unknown:
        raise PipelineError(
            f"[{ctx.module}.{ctx.step}] в {REDO} нет таких картинок: {', '.join(unknown)}. "
            "Пишите имя файла без .png — например, 5 или 12_2"
        )

    review = {}
    if ctx.path(REVIEW).exists():
        review = json.loads(ctx.path(REVIEW).read_text(encoding="utf-8")).get("images", {})
    originals = {
        base: original
        for shot in board.shots
        for base, original in zip(file_names(shot), shot.image_prompts or [shot.visual])
    }
    fixes = read_fixes(ctx)
    log.info("[%s.%s] переделка по %s: %s", ctx.module, ctx.step, REDO, ", ".join(sorted(wanted, key=natural)))

    # Сначала все новые описания (здесь может понадобиться нейросеть), и только потом — перенос старых файлов.
    # Остановка посреди описаний ничего не меняет: redo.txt останется, следующий запуск начнёт заново.
    old_files = {}
    for base, wish in sorted(wanted.items(), key=lambda item: natural(item[0])):
        old_files[base] = next((ctx.path(base + ext) for ext in IMAGE_EXTENSIONS if ctx.path(base + ext).exists()), None)
        notes = review.get(base) or {}
        if wish:
            new = rewrite_description(ctx, board, base, descriptions[base], wish, notes.get("problems") or [], old_files[base])
        elif notes.get("fixed_prompt"):
            new = notes["fixed_prompt"]
        else:
            new = descriptions[base]  # без пожелания и без совета — тот же промпт, генератор даст другой вариант
        descriptions[base] = new
        if new != originals[base]:
            fixes[base] = {"from": originals[base], "to": new}

    # Поправки сохраняются и redo.txt очищается до генерации: старых файлов уже нет,
    # поэтому даже оборванный запуск доделает эти картинки с новыми описаниями.
    ctx.write(FIXES, json.dumps(fixes, ensure_ascii=False, indent=2))
    for old_file in old_files.values():
        if old_file:
            ctx.keep_old(old_file)
    write_redo(ctx)


class RedoPrompt(BaseModel):
    prompt: str = Field(description="Новое описание сцены для генератора картинок, на английском, 30–70 слов")


def rewrite_description(ctx: StepContext, board, base: str, description: str, wish: str, problems: list[str], image) -> str:
    """Нейросеть смотрит на текущую картинку и переписывает описание сцены под пожелание автора."""
    system, user = ctx.render_prompt(
        template=REDO_PROMPT,
        name=base,
        description=description,
        wish=wish,
        problems=problems,
        style=board.style,
        has_image=image is not None,
    )
    images = [Image(image.read_bytes(), mime_type(image.read_bytes()))] if image else None
    return ctx.ask_json(system, user, RedoPrompt, images=images).prompt.strip()
