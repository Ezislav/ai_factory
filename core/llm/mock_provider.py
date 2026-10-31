"""Нейросеть-заглушка без сети и ключей: демо-текст или JSON, собранный по схеме. Для проверки пайплайна."""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm.base import LLMProvider, LLMResponse, text_of

DEMO_SCRIPT = """\
# Демо-сценарий (mock)

## 1. Хук
Это текст диктора, созданный заглушкой без обращения к нейросети.

## 2. Вывод
Пайплайн работает — можно подключать настоящую модель.
"""


def example_from_schema(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    match schema.get("type"):
        case "object":
            return {key: example_from_schema(sub) for key, sub in (schema.get("properties") or {}).items()}
        case "array":
            return [example_from_schema(schema.get("items") or {})]
        case "integer":
            return 5
        case "number":
            return 1.0
        case "boolean":
            return False
    return "пример (mock)"


class MockProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "mock"

    def complete(self, system, messages, json_schema=None) -> LLMResponse:
        if not json_schema:
            return LLMResponse(text=DEMO_SCRIPT, model="mock")
        answer = example_from_schema(json_schema)
        prompt = text_of(messages[0]["content"])
        if "shots" in (json_schema.get("properties") or {}):
            # Раскадровка: по кадру на каждую пронумерованную фразу «[N]» из промпта.
            numbers = re.findall(r"^\[(\d+)\]", prompt, re.M)
            answer["shots"] = [{**answer["shots"][0], "first": int(n), "last": int(n)} for n in numbers]
            # Сколько картинок в кадре, промпт говорит строкой «image_prompts — N промпта».
            count = re.search(r"image_prompts — (\d+)", prompt)
            for shot in answer["shots"]:
                if "image_prompts" in shot:
                    shot["image_prompts"] = [f"Demo picture {i} of shot {shot['first']}" for i in range(1, int(count[1]) + 1 if count else 2)]
        return LLMResponse(text=json.dumps(answer, ensure_ascii=False), model="mock")
