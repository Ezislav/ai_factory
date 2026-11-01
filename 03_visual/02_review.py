"""Проверка картинок: нейросеть смотрит на каждую картинку → 03_visual/review.json, review.html и redo.txt.

Для каждой картинки нейросеть видит саму картинку, текст диктора кадра, описание сцены и стиль ролика,
ставит оценку 1–10, перечисляет проблемы (не та сцена, дефекты, надписи, чужой стиль, правила YouTube)
и для плохих (оценка ниже review_min_score) предлагает исправленное описание.

Сама проверка ничего не переделывает. review.html — страница с картинками и замечаниями, её открывают
в браузере. В redo.txt проверка выписывает плохие картинки закомментированными: автор сам решает,
что переделать, убирая # (и, если хочет, дописывая пожелание). Переделку делает шаг 01_generate
при следующем запуске программы.

Проверенная картинка второй раз не проверяется, пока не изменится её файл.
"""

import hashlib
import html
import logging

from pydantic import BaseModel, Field

from core.llm import Image, LLMError
from core.media.base import mime_type
from core.pipeline import load_step
from core.step import PipelineError, StepContext
from core.structured import StructuredOutputError

generate = load_step("visual", "generate")

OUTPUT = generate.REVIEW
PAGE = "review.html"
SHOW = PAGE

log = logging.getLogger(__name__)


class Verdict(BaseModel):
    """Ответ нейросети по одной картинке."""

    score: int = Field(description="Оценка от 1 до 10")
    problems: list[str] = Field(description="Что не так — коротко, по-русски; пустой список, если всё хорошо")
    fixed_prompt: str = Field(
        description="Если картинку нужно переделать: исправленное описание сцены на английском, иначе пустая строка"
    )


class ImageReview(BaseModel):
    file: str
    shot: int
    hash: str  # по нему видно, что файл поменялся и его надо проверить заново
    score: int | None = None  # None — проверить не получилось
    problems: list[str] = []
    fixed_prompt: str = ""
    error: str = ""


class ReviewSet(BaseModel):
    min_score: int
    images: dict[str, ImageReview]  # имя без расширения → отзыв


def check_verdict(verdict: Verdict) -> None:
    if not 1 <= verdict.score <= 10:
        raise ValueError(f"score должен быть от 1 до 10, а не {verdict.score}")


def file_hash(path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def base_of(name: str) -> str:
    return name.rsplit(".", 1)[0]


def done(ctx: StepContext) -> bool:
    """Готово, если каждая текущая картинка уже проверена (и с тех пор не менялась)."""
    images = ctx.read_json(generate.OUTPUT, generate.ImageSet)
    review = ctx.read_json(OUTPUT, ReviewSet)
    if images is None or review is None:
        return False
    for shot in images.shots:
        for name in shot.files:
            entry = review.images.get(base_of(name))
            path = ctx.path(name)
            if entry is None or not path.exists() or entry.hash != file_hash(path):
                return False
    return True


def run(ctx: StepContext) -> None:
    images = ctx.read_json(generate.OUTPUT, generate.ImageSet)
    if images is None:
        raise PipelineError(f"[{ctx.module}.{ctx.step}] нет {generate.OUTPUT} — картинки делает шаг generate")
    board = generate.read_storyboard(ctx)
    shots = {shot.id: shot for shot in board.shots}
    min_score = ctx.params.get("review_min_score", 7)
    previous = ctx.read_json(OUTPUT, ReviewSet)
    previous = previous.images if previous else {}

    results: dict[str, ImageReview] = {}
    todo = []
    for item in images.shots:
        descriptions = item.descriptions or item.prompts
        for name, description in zip(item.files, descriptions):
            base, digest = base_of(name), file_hash(ctx.path(name))
            old = previous.get(base)
            if old and old.hash == digest and not old.error:
                results[base] = old
            else:
                todo.append((base, name, item.id, description, digest))
    if len(results):
        log.info("[%s.%s] уже проверены: %d, осталось: %d", ctx.module, ctx.step, len(results), len(todo))

    def review(job) -> ImageReview:
        base, name, shot_id, description, digest = job
        shot = shots[shot_id]
        system, user = ctx.render_prompt(
            title=board.title,
            shot=shot_id,
            text=shot.text,
            description=description,
            style=board.style,
            min_score=min_score,
        )
        data = ctx.path(name).read_bytes()
        try:
            verdict = ctx.ask_json(system, user, Verdict, check=check_verdict, images=[Image(data, mime_type(data))])
        except (LLMError, StructuredOutputError) as e:
            log.warning("[%s.%s] %s: проверить не получилось — %s", ctx.module, ctx.step, name, e)
            return ImageReview(file=name, shot=shot_id, hash=digest, error=str(e))
        log.info("[%s.%s] %s: %d/10", ctx.module, ctx.step, name, verdict.score)
        return ImageReview(
            file=name,
            shot=shot_id,
            hash=digest,
            score=verdict.score,
            problems=verdict.problems,
            fixed_prompt=verdict.fixed_prompt.strip() if verdict.score < min_score else "",
        )

    if todo:
        log.info("[%s.%s] проверка картинок: %d, нейросеть: %s (%s)", ctx.module, ctx.step, len(todo), ctx.provider.name, ctx.provider.model)
        for job, result in zip(todo, generate.run_parallel(ctx, todo, review, ctx.params.get("workers", 4))):
            results[job[0]] = result

    ordered = dict(sorted(results.items(), key=lambda item: generate.natural(item[0])))
    report = ReviewSet(min_score=min_score, images=ordered)
    ctx.write_json(OUTPUT, report)
    ctx.write(PAGE, render_page(ctx, board, images, report))

    bad = [(base, entry) for base, entry in ordered.items() if entry.score is not None and entry.score < min_score]
    failed = [base for base, entry in ordered.items() if entry.error]
    if generate.read_redo(ctx):
        log.info("[%s.%s] в %s уже отмечены картинки — файл не меняю", ctx.module, ctx.step, generate.REDO)
    else:
        generate.write_redo(ctx, [(base, f"{entry.score}/10: {'; '.join(entry.problems)}") for base, entry in bad])

    log.info(
        "[%s.%s] проверено картинок: %d, плохих (ниже %d): %d%s. Отчёт: %s",
        ctx.module, ctx.step, len(ordered), min_score, len(bad),
        f", не проверены: {len(failed)}" if failed else "", ctx.path(PAGE),
    )
    if not images.complete and not ctx.settings.step_by_step:
        raise PipelineError(
            generate.trial_message(ctx, len(images.shots), len(board.shots), len(ordered))
            + f". Отчёт проверки — {ctx.path(PAGE).relative_to(ctx.project.dir)}"
        )


# --- review.html ---

PAGE_STYLE = """
:root { --bg: #f6f6f4; --card: #fff; --text: #1d1d1b; --muted: #6b6b66; --line: #e2e2dc;
        --good: #1f7a3f; --bad: #b3261e; --bad-bg: #fdecea; --code: #f0f0ec; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --card: #22221f; --text: #ecece8; --muted: #a0a09a; --line: #34342f;
          --good: #6fcf8e; --bad: #ff8a80; --bad-bg: #3a1f1d; --code: #2b2b27; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px 48px; background: var(--bg); color: var(--text);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
.summary { color: var(--muted); margin: 0 0 16px; }
.howto { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; margin-bottom: 24px; }
.howto code, .prompt { background: var(--code); border-radius: 4px; padding: 1px 5px; }
.filters { margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.card.bad { border-color: var(--bad); box-shadow: 0 0 0 1px var(--bad); }
.card img { display: block; width: 100%; height: auto; background: var(--code); }
.body { padding: 12px 14px 14px; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.name { font-weight: 600; font-size: 17px; }
.score { font-weight: 700; color: var(--good); white-space: nowrap; }
.bad .score { color: var(--bad); }
.time { color: var(--muted); font-size: 13px; }
.text { margin: 6px 0; font-style: italic; }
ul { margin: 6px 0; padding-left: 20px; }
.bad ul { background: var(--bad-bg); border-radius: 6px; padding: 6px 8px 6px 26px; }
details { margin-top: 6px; color: var(--muted); font-size: 13px; }
details p { margin: 4px 0; color: var(--text); }
.old a { color: var(--muted); }
body.only-bad .card:not(.bad) { display: none; }
"""


def render_page(ctx: StepContext, board, images, report: ReviewSet) -> str:
    esc = html.escape
    shots = {shot.id: shot for shot in board.shots}
    old_dir = ctx.path(generate.OLD)
    old_files = sorted(p.name for p in old_dir.iterdir()) if old_dir.exists() else []
    cards = []
    for item in images.shots:
        shot = shots[item.id]
        for name, description in zip(item.files, item.descriptions or item.prompts):
            base = base_of(name)
            entry = report.images.get(base)
            bad = entry is not None and entry.score is not None and entry.score < report.min_score
            if entry is None or entry.error:
                score = "не проверена"
                notes = f"<p>{esc(entry.error)}</p>" if entry else ""
            else:
                score = f"{entry.score}/10"
                notes = "<ul>" + "".join(f"<li>{esc(p)}</li>" for p in entry.problems) + "</ul>" if entry.problems else ""
            fix = (
                f"<details><summary>Как исправить (совет проверки)</summary><p>{esc(entry.fixed_prompt)}</p></details>"
                if entry and entry.fixed_prompt
                else ""
            )
            olds = [f for f in old_files if f.startswith(base + "_v")]
            old = (
                '<div class="old time">Прежние варианты: ' + ", ".join(f'<a href="{generate.OLD}/{f}">{f}</a>' for f in olds) + "</div>"
                if olds
                else ""
            )
            cards.append(f"""
<article class="card{' bad' if bad else ''}" id="{base}">
  <a href="{esc(name)}"><img src="{esc(name)}" alt="Картинка {base}" loading="lazy"></a>
  <div class="body">
    <div class="head"><span class="name">{base}</span><span class="score">{score}</span></div>
    <div class="time">Кадр {item.id} · {shot.start:.1f}–{shot.end:.1f} с · {esc(shot.block)}</div>
    <p class="text">«{esc(shot.text)}»</p>
    {notes}{fix}
    <details><summary>Описание сцены</summary><p>{esc(description)}</p></details>
    {old}
  </div>
</article>""")

    checked = [entry for entry in report.images.values() if entry.score is not None]
    bad_count = sum(entry.score < report.min_score for entry in checked)
    partial = "" if images.complete else f" Пробный запуск: кадров {len(images.shots)} из {len(board.shots)}."
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Проверка картинок</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
<main>
  <h1>Проверка картинок: {esc(board.title)}</h1>
  <p class="summary">Картинок: {len(report.images)}, проверено: {len(checked)}, плохих (оценка ниже {report.min_score}): {bad_count}.{partial}
  Стиль ролика: {esc(board.style)}</p>
  <div class="howto">
    <b>Как переделать картинку.</b> Откройте <code>{generate.REDO}</code> в этой же папке и впишите имя картинки
    (как на карточке, например <code>5</code>), можно с пожеланием: <code>5: сделай закат ярче</code>.
    Плохие картинки там уже выписаны — уберите <code>#</code> в начале строки. Затем запустите программу снова:
    отмеченные картинки сделаются заново, прежние варианты уйдут в папку <code>{generate.OLD}</code>.
  </div>
  <label class="filters"><input type="checkbox" onchange="document.body.classList.toggle('only-bad', this.checked)"> Только плохие</label>
  <section class="grid">{''.join(cards)}
  </section>
</main>
</body>
</html>"""
