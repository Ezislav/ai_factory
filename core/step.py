"""Контекст шага: параметры модуля, промпт, нейросеть и файлы проекта.

Шаг — файл <NN_модуль>/<NN_шаг>.py (например, 01_scenario/02_factcheck.py) с двумя объектами:
  OUTPUT — файл результата в папке модуля в проекте (01_scenario/...); если он уже есть, шаг пропускается;
  run(ctx: StepContext) — сама работа.
Необязательно: REQUIRED = True — шаг нельзя выключить в config.yaml.
              done(ctx) -> bool — шаг сам решает, готов ли он (вместо «файл OUTPUT уже есть»).
Промпт шага, если он нужен, лежит рядом: 01_scenario/02_factcheck.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from core.config import Settings, module_dir, step_file
from core.llm import Image, LLMProvider, LLMResponse, create_provider
from core.media import Media, MediaProvider, create_media_provider
from core.project import Project
from core.prompts import load_prompt
from core.structured import Check, ask_json, ask_text

T = TypeVar("T", bound=BaseModel)
ProviderFactory = Callable[[str | None], LLMProvider]


class PipelineError(RuntimeError):
    pass


class ProviderPool:
    """Одна копия каждой нейросети на весь запуск."""

    def __init__(self, settings: Settings, factory: ProviderFactory | None = None):
        self.settings = settings
        self.factory = factory or (lambda name: create_provider(settings.llm, name))
        self._cache: dict[str, LLMProvider] = {}
        self._media: dict[tuple[str, str], MediaProvider] = {}

    def get(self, name: str | None = None) -> LLMProvider:
        """--provider > указанная в config.yaml > llm.default."""
        name = self.settings.provider_override or name or None
        key = name or ""
        if key not in self._cache:
            self._cache[key] = self.factory(name)
        return self._cache[key]

    def media(self, name: str | None, kind: str) -> MediaProvider:
        """Генератор картинок или видео. --provider mock подменяет его на mock_image / mock_video, если такой есть."""
        override = self.settings.provider_override
        if override and f"{override}_{kind}" in (self.settings.media.get("providers") or {}):
            name = f"{override}_{kind}"
        key = (name or "", kind)
        if key not in self._media:
            self._media[key] = create_media_provider(self.settings.media, name, kind)
        return self._media[key]


class StepContext:
    def __init__(
        self,
        project: Project,
        settings: Settings,
        module: str,
        step: str,
        steps: list[str],
        providers: ProviderPool,
    ):
        self.project = project
        self.settings = settings
        self.module = module
        self.step = step
        self.steps = steps  # все шаги модуля по порядку
        self.providers = providers

    @property
    def config(self) -> dict[str, Any]:
        return self.settings.module(self.module)

    @property
    def params(self) -> dict[str, Any]:
        """params модуля из config.yaml, перекрытые params ролика (--set)."""
        return self.params_of(self.module)

    def params_of(self, module: str) -> dict[str, Any]:
        """params другого модуля — например, темп диктора из scenario для раскадровки."""
        return {**(self.settings.module(module).get("params") or {}), **self.project.params}

    @property
    def topic(self) -> str:
        return self.project.topic

    @property
    def provider(self) -> LLMProvider:
        step_settings = (self.config.get("step_settings") or {}).get(self.step) or {}
        return self.providers.get(step_settings.get("provider") or self.config.get("provider"))

    def render_prompt(self, template: str | None = None, **extra: Any) -> tuple[str, str]:
        """<NN_модуль>/<NN_шаг>.md с параметрами модуля, темой и extra.
        template — другой файл промпта из папки модуля (например, "01_redo.md")."""
        if template:
            path = module_dir(self.module) / template
        else:
            path = step_file(self.module, self.step, ".md") or module_dir(self.module) / f"NN_{self.step}.md"
        prompt = load_prompt(path)
        return prompt.render({**self.params, "topic": self.topic, **extra})

    def media(self, kind: str) -> MediaProvider:
        """Генератор картинок (kind="image") или видео ("video") из {kind}_provider модуля в config.yaml."""
        return self.providers.media(self.config.get(f"{kind}_provider"), kind)

    def log_media(self, provider: MediaProvider, media: Media, **details: Any) -> None:
        self.project.log(self.module, {"step": f"{self.module}.{self.step}", "provider": provider.name, "model": media.model, **details})

    def ask_text(self, system: str, prompt: str) -> str:
        return ask_text(self.provider, system, prompt, on_response=self._log)

    def ask_json(
        self, system: str, prompt: str, model_cls: type[T], check: Check | None = None, images: list[Image] | None = None
    ) -> T:
        max_attempts = self.config.get("max_attempts", 3)
        return ask_json(self.provider, system, prompt, model_cls, max_attempts, on_response=self._log, check=check, images=images)

    def _log(self, response: LLMResponse) -> None:
        self.project.log_usage(self.module, f"{self.module}.{self.step}", self.provider.name, response)

    def path(self, name: str, module: str | None = None) -> Path:
        """Файл в папке модуля в проекте; module — чтобы взять файл другого модуля (по умолчанию свой)."""
        return self.project.path(module or self.module, name)

    def read(self, name: str, module: str | None = None) -> str:
        path = self.path(name, module)
        if not path.exists():
            raise PipelineError(f"[{self.module}.{self.step}] нет файла {path} — его создаёт один из предыдущих шагов")
        return path.read_text(encoding="utf-8")

    def write_bytes(self, name: str, data: bytes) -> Path:
        """Запись целиком или никак: при Ctrl+C или сбое не останется недописанного файла,
        который следующий запуск принял бы за готовый."""
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".part")
        temp.write_bytes(data)
        temp.replace(path)
        return path

    def write(self, name: str, text: str) -> Path:
        return self.write_bytes(name, (text.strip() + "\n").encode("utf-8"))

    def write_json(self, name: str, model: BaseModel) -> Path:
        return self.write_bytes(name, model.model_dump_json(indent=2).encode("utf-8"))

    def read_json(self, name: str, model_cls: type[T], module: str | None = None) -> T | None:
        path = self.path(name, module)
        return model_cls.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
