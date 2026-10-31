"""Ответы нейросети: чистый текст или JSON по pydantic-модели с проверкой и повтором."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from core.llm import Image, LLMProvider, LLMResponse, interruptible

T = TypeVar("T", bound=BaseModel)
OnResponse = Callable[[LLMResponse], None]
Check = Callable[[Any], None]  # дополнительная проверка ответа: ValueError → повторный запрос

FORMAT_INSTRUCTIONS = """\
Верни ответ строго в формате JSON — один объект, без пояснений и без markdown — по этой JSON-схеме:
{schema}"""

RETRY_INSTRUCTIONS = "Ответ не прошёл проверку: {error}\nИсправь ошибки и верни полный JSON целиком."

log = logging.getLogger(__name__)


class StructuredOutputError(RuntimeError):
    pass


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].split("/")[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k not in ("$defs", "title")}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def json_schema_for(model_cls: type[BaseModel]) -> dict[str, Any]:
    """JSON-схема без $ref и title — так её понимают и structured outputs, и модели в промпте."""
    schema = model_cls.model_json_schema()
    return _inline_refs(schema, schema.get("$defs", {}))


def strip_fences(text: str) -> str:
    """Убрать обёртку ```...```, если модель завернула в неё весь ответ."""
    return re.sub(r"^```[\w-]*\s*\n|\n?\s*```$", "", text.strip())


def parse_json(text: str, model_cls: type[T]) -> T:
    text = strip_fences(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON-объекта")
    return model_cls.model_validate(json.loads(text[start : end + 1]))


def ask_text(provider: LLMProvider, system: str, prompt: str, on_response: OnResponse | None = None) -> str:
    response = interruptible(provider.complete, system, [{"role": "user", "content": prompt}])
    if on_response:
        on_response(response)
    text = strip_fences(response.text)
    if not text:
        raise StructuredOutputError(f"[{provider.name}] пустой ответ")
    return text


def ask_json(
    provider: LLMProvider,
    system: str,
    prompt: str,
    model_cls: type[T],
    max_attempts: int = 3,
    on_response: OnResponse | None = None,
    check: Check | None = None,
    images: list[Image] | None = None,
) -> T:
    """images — картинки, которые нейросеть должна увидеть (идут перед текстом запроса)."""
    schema = json_schema_for(model_cls)
    text = f"{prompt}\n\n{FORMAT_INSTRUCTIONS.format(schema=json.dumps(schema, ensure_ascii=False, indent=2))}"
    messages = [{"role": "user", "content": [*images, text] if images else text}]
    for attempt in range(1, max_attempts + 1):
        response = interruptible(provider.complete, system, messages, json_schema=schema)
        if on_response:
            on_response(response)
        try:
            result = parse_json(response.text, model_cls)
            if check:
                check(result)
            return result
        except ValueError as e:
            log.warning("Ответ не прошёл проверку (попытка %d/%d): %s", attempt, max_attempts, e)
            messages += [
                {"role": "assistant", "content": response.text},
                {"role": "user", "content": RETRY_INSTRUCTIONS.format(error=e)},
            ]
    raise StructuredOutputError(f"[{provider.name}] не удалось получить корректный JSON за {max_attempts} попыток")
