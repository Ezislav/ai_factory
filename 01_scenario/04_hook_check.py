"""Оценка хука — первых секунд ролика — и вариант получше."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.pipeline import load_step
from core.step import StepContext

DRAFT = load_step("scenario", "generate").OUTPUT

OUTPUT = "hook_check.md"
REPORT = "hook_check.json"


class HookReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(description="Оценка хука от 1 до 10")
    strengths: list[str] = Field(description="Что в хуке работает")
    problems: list[str] = Field(description="Что мешает удержать зрителя")
    improved_hook: str = Field(description="Переписанный текст хука того же объёма, в стиле сценария")

    @field_validator("score")
    @classmethod
    def _score_range(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("score должен быть от 1 до 10")
        return value

    def to_markdown(self, min_score: int) -> str:
        verdict = "проходит" if self.score >= min_score else f"ниже порога {min_score} — будет переписан на шаге revise"
        lines = [f"# Проверка хука: {self.score}/10 ({verdict})", "", "## Сильные стороны"]
        lines += [f"- {item}" for item in self.strengths] or ["- —"]
        lines += ["", "## Проблемы"]
        lines += [f"- {item}" for item in self.problems] or ["- —"]
        lines += ["", "## Вариант хука", "", self.improved_hook, ""]
        return "\n".join(lines)


def run(ctx: StepContext) -> None:
    system, user = ctx.render_prompt(script=ctx.read(DRAFT))
    report = ctx.ask_json(system, user, HookReport)
    ctx.write_json(REPORT, report)
    ctx.write(OUTPUT, report.to_markdown(min_score=ctx.params.get("hook_min_score", 8)))
