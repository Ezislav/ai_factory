"""Черновик сценария по теме и параметрам формата → scenario/draft.md."""

from core.step import StepContext

OUTPUT = "draft.md"
REQUIRED = True  # без черновика проверять и дорабатывать нечего


def run(ctx: StepContext) -> None:
    system, user = ctx.render_prompt()
    ctx.write(OUTPUT, ctx.ask_text(system, user))
