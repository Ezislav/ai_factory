"""Фактчек черновика: ошибочные, спорные и устаревшие утверждения с исправлениями.

Здесь же формат отчёта со списком замечаний (CheckReport) — его использует и youtube_policy.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.pipeline import load_step
from core.step import StepContext

DRAFT = load_step("scenario", "generate").OUTPUT

TITLE = "Фактчек"
OUTPUT = "factcheck.md"
REPORT = "factcheck.json"

SEVERITY_LABELS = {"critical": "критично", "major": "важно", "minor": "мелочь"}


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(description="Дословный фрагмент сценария, к которому относится замечание")
    problem: str = Field(description="В чём проблема")
    fix: str = Field(description="Как исправить: готовая формулировка или конкретное действие")
    severity: Literal["critical", "major", "minor"] = Field(
        description="critical — нельзя публиковать как есть, major — стоит исправить, minor — по желанию"
    )

    @property
    def severity_label(self) -> str:
        return SEVERITY_LABELS[self.severity]


class CheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="Общий вывод в одном-двух предложениях")
    issues: list[Issue] = Field(description="Замечания; пустой список, если всё в порядке")

    def to_markdown(self, title: str) -> str:
        lines = [f"# {title}", "", self.summary, ""]
        if not self.issues:
            lines.append("Замечаний нет.")
        for i, issue in enumerate(self.issues, start=1):
            lines += [
                f"## {i}. {issue.severity_label}",
                "",
                f"> {issue.quote}",
                "",
                f"**Проблема:** {issue.problem}",
                "",
                f"**Исправление:** {issue.fix}",
                "",
            ]
        return "\n".join(lines)


def run_check(ctx: StepContext, title: str, output: str, report_path: str) -> None:
    """Черновик → промпт 01_scenario/NN_<шаг>.md → отчёт .json (для revise) и .md (для чтения)."""
    system, user = ctx.render_prompt(script=ctx.read(DRAFT))
    report = ctx.ask_json(system, user, CheckReport)
    ctx.write_json(report_path, report)
    ctx.write(output, report.to_markdown(title))


def run(ctx: StepContext) -> None:
    run_check(ctx, TITLE, OUTPUT, REPORT)
