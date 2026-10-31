"""Проверка черновика на правила YouTube: правила сообщества, монетизация, авторские права, дисклеймеры."""

from core.pipeline import load_step
from core.step import StepContext

run_check = load_step("scenario", "factcheck").run_check

TITLE = "Правила YouTube"
OUTPUT = "youtube_policy.md"
REPORT = "youtube_policy.json"


def run(ctx: StepContext) -> None:
    run_check(ctx, TITLE, OUTPUT, REPORT)
