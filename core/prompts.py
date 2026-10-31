"""Промпты: <NN_модуль>/<NN_шаг>.md рядом с файлом шага — Markdown с переменными Jinja2.

Необязательный YAML-заголовок между строками `---` задаёт system-промпт:

    ---
    system: |
      Ты — ...
    ---
    Текст запроса с {{ topic }} и другими параметрами.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
import yaml

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True)


class PromptError(ValueError):
    pass


@dataclass
class Prompt:
    name: str
    system: str
    user: str

    def render(self, context: dict[str, Any]) -> tuple[str, str]:
        try:
            return (
                _env.from_string(self.system).render(context).strip(),
                _env.from_string(self.user).render(context).strip(),
            )
        except jinja2.UndefinedError as e:
            raise PromptError(f"Промпт {self.name}: {e.message} — задайте параметр в config.yaml или через --set") from e
        except jinja2.TemplateSyntaxError as e:
            raise PromptError(f"Промпт {self.name}, строка {e.lineno}: {e.message}") from e


def load_prompt(path: Path) -> Prompt:
    if not path.exists():
        raise PromptError(f"Нет файла промпта {path}")
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    if match := _FRONT_MATTER.match(text):
        meta = yaml.safe_load(match.group(1)) or {}
        text = text[match.end() :]
    return Prompt(name=f"{path.parent.name}/{path.name}", system=meta.get("system", ""), user=text)
