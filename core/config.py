"""Настройки: config.yaml в корне (модули, шаги, нейросети, генераторы картинок и видео), ключи — в .env рядом с ним."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"
PROJECTS_DIR = PROJECT_ROOT / "projects"


_NUMBER_PREFIX = re.compile(r"^\d+[._ -]*")


class ConfigError(ValueError):
    pass


def find_numbered(parent: Path, name: str, suffix: str = "") -> Path | None:
    """Файл или папка с номером впереди или без: 01_scenario, 02_factcheck.py, scenario, factcheck.py.

    Номер нужен только для порядка в Проводнике — порядок запуска задаёт config.yaml.
    """
    pattern = re.compile(rf"^(?:\d+[._ -]*)?{re.escape(name)}{re.escape(suffix)}$")
    matches = [p for p in parent.iterdir() if pattern.match(p.name)]
    if len(matches) > 1:
        raise ConfigError(f"Несколько файлов для «{name}{suffix}» в {parent.name}: {', '.join(p.name for p in matches)}")
    return matches[0] if matches else None


def module_dir(module: str) -> Path:
    """Папка модуля в корне проекта: 01_scenario или просто scenario."""
    path = find_numbered(PROJECT_ROOT, module)
    if path is None or not path.is_dir():
        raise ConfigError(f"Не найдена папка модуля {module} в {PROJECT_ROOT} (например, 01_{module})")
    return path


def step_file(module: str, step: str, suffix: str) -> Path | None:
    """Файл шага в папке модуля: 01_scenario/02_factcheck.py, его промпт — 01_scenario/02_factcheck.md."""
    return find_numbered(module_dir(module), step, suffix)


def read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Settings:
    data: dict[str, Any]
    provider_override: str | None = None  # --provider: одна нейросеть на весь запуск
    step_by_step: bool = False  # --step: остановка после каждого этапа, пока автор не проверит результат

    @property
    def pipeline(self) -> list[str]:
        return list(self.data.get("pipeline") or [])

    @property
    def llm(self) -> dict[str, Any]:
        return self.data.get("llm") or {}

    @property
    def media(self) -> dict[str, Any]:
        return self.data.get("media") or {}

    def module(self, name: str) -> dict[str, Any]:
        return (self.data.get("modules") or {}).get(name) or {}


def bare_name(name: str) -> str:
    """01_scenario → scenario: номер в config.yaml — только для порядка глазами, код работает с именами."""
    return _NUMBER_PREFIX.sub("", str(name))


def _strip_numbers(data: dict) -> dict:
    """Имена модулей в pipeline и modules можно писать с номером (01_scenario) или без."""
    data = dict(data)
    data["pipeline"] = [bare_name(name) for name in data.get("pipeline") or []]
    modules = {}
    for key, value in (data.get("modules") or {}).items():
        name = bare_name(key)
        if name in modules:
            raise ConfigError(f"Модуль {name} описан в config.yaml дважды (modules.{key})")
        modules[name] = value
    data["modules"] = modules
    return data


def load_settings(path: Path = CONFIG_FILE) -> Settings:
    if not path.exists():
        raise ConfigError(f"Нет файла настроек {path}")
    load_dotenv(ENV_FILE)
    return Settings(data=_strip_numbers(read_yaml(path)))
