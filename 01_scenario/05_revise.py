"""Доработка: черновик + замечания проверок → итоговый сценарий scenario/script.md (выход блока)."""

import logging

from core.pipeline import load_step
from core.step import StepContext

factcheck = load_step("scenario", "factcheck")
youtube_policy = load_step("scenario", "youtube_policy")
hook_check = load_step("scenario", "hook_check")
DRAFT = load_step("scenario", "generate").OUTPUT

OUTPUT = "script.md"
REQUIRED = True  # script.md — выход блока, его читают следующие модули
CHECKS = {"factcheck": factcheck, "youtube_policy": youtube_policy}

log = logging.getLogger(__name__)


def run(ctx: StepContext) -> None:
    draft = ctx.read(DRAFT)

    # Учитываются только проверки, включённые в config.yaml, — старые отчёты выключенных шагов игнорируются.
    issues = []
    for name, check in CHECKS.items():
        report = ctx.read_json(check.REPORT, factcheck.CheckReport) if name in ctx.steps else None
        issues += [(check.TITLE, issue) for issue in (report.issues if report else [])]

    min_score = ctx.params.get("hook_min_score", 8)
    hook = ctx.read_json(hook_check.REPORT, hook_check.HookReport) if "hook_check" in ctx.steps else None
    weak_hook = hook if hook and hook.score < min_score else None

    if not issues and not weak_hook:
        log.info("[scenario.revise] замечаний нет — черновик становится итоговым сценарием")
        ctx.write(OUTPUT, draft)
        return

    system, user = ctx.render_prompt(script=draft, issues=issues, hook=weak_hook, hook_min_score=min_score)
    ctx.write(OUTPUT, ctx.ask_text(system, user))
