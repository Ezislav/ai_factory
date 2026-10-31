"""Запуск пайплайна: модули по порядку из config.yaml, внутри модуля — шаги по порядку.

Модуль — папка в корне проекта с номером или без: 01_scenario/ или scenario/. Шаги — файлы в ней
(см. core/step.py), их порядок и переключатели — modules → <NN_модуль> → steps в config.yaml ({шаг: true/false}).
Шаг пропускается, если его OUTPUT уже есть. После пересчёта любого шага пересчитываются все следующие — в этом модуле и в следующих.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

from core.config import PROJECT_ROOT, Settings, module_dir, step_file
from core.project import Project
from core.step import PipelineError, ProviderFactory, ProviderPool, StepContext

__all__ = ["PipelineError", "declared_steps", "load_step", "module_dir", "module_steps", "resolve_from", "run_pipeline"]

log = logging.getLogger(__name__)

_loaded: dict[tuple[str, str], ModuleType] = {}
_NUMBERED = re.compile(r"^(\d+)[._ -]*(.+)$")  # 02_storyboard → 02, storyboard


def declared_steps(settings: Settings, module: str) -> dict[str, bool]:
    """steps модуля из config.yaml по порядку: {шаг: включён}."""
    steps = settings.module(module).get("steps")
    if not steps:
        raise PipelineError(f"Для модуля {module} не заданы steps в config.yaml")
    if isinstance(steps, list):  # краткая запись [a, b] — все включены
        return {name: True for name in steps}
    for name, enabled in steps.items():
        if not isinstance(enabled, bool):
            raise PipelineError(f"{module_dir(module).name} → steps → {name} в config.yaml: ожидается true или false, получено {enabled!r}")
    return dict(steps)


def module_steps(settings: Settings, module: str) -> list[str]:
    """Включённые шаги модуля по порядку."""
    return [name for name, enabled in declared_steps(settings, module).items() if enabled]


def check_required(settings: Settings, module: str) -> None:
    """Шаг с REQUIRED = True нельзя выключить — без него модуль не выдаст результат."""
    for name, enabled in declared_steps(settings, module).items():
        if not enabled and getattr(load_step(module, name), "REQUIRED", False):
            raise PipelineError(f"Шаг {module}.{name} обязательный — включите его: steps.{name}: true")


def load_step(module: str, step: str) -> ModuleType:
    """Загрузить шаг по пути к файлу — имена папок с номером (01_scenario) не годятся для import.

    Шаги могут брать друг у друга константы и функции: load_step("scenario", "generate").OUTPUT
    """
    if (module, step) in _loaded:
        return _loaded[(module, step)]
    path = step_file(module, step, ".py")
    if path is None:
        raise PipelineError(f"Шаг {module}.{step} не найден: нет файла {module_dir(module).name}/NN_{step}.py")

    name = f"steps.{module}.{step}"
    spec = importlib.util.spec_from_file_location(name, path)
    code = importlib.util.module_from_spec(spec)
    sys.modules[name] = code  # нужно pydantic-моделям внутри шага
    try:
        spec.loader.exec_module(code)
    except BaseException:
        del sys.modules[name]
        raise
    if not hasattr(code, "OUTPUT") or not hasattr(code, "run"):
        raise PipelineError(f"В {path.relative_to(PROJECT_ROOT)} должны быть OUTPUT и run(ctx)")
    _loaded[(module, step)] = code
    return code


def _numbered(parent: Path, number: str) -> list[tuple[str, Path]]:
    """Файлы и папки с номером number впереди: '2' находит 02_storyboard → [('storyboard', путь)]."""
    found = []
    for path in parent.iterdir():
        match = _NUMBERED.match(path.stem if path.is_file() else path.name)
        if match and int(match.group(1)) == int(number):
            found.append((match.group(2), path))
    return found


def _module_by_number(settings: Settings, number: str, value: str) -> str:
    folders = [(name, path) for name, path in _numbered(PROJECT_ROOT, number) if path.is_dir()]
    if not folders:
        raise PipelineError(f"Начать с {value}: нет папки модуля с номером {number}")
    name, path = folders[0]
    if name not in settings.pipeline:
        raise PipelineError(
            f"Начать с {value}: модуль {path.name} выключен — добавьте {path.name} в pipeline в config.yaml"
        )
    return name


def _step_by_number(settings: Settings, module: str, number: str, value: str) -> str:
    files = {name for name, path in _numbered(module_dir(module), number) if path.suffix == ".py"}
    enabled = [step for step in module_steps(settings, module) if step in files]
    if enabled:
        return enabled[0]
    if files & set(declared_steps(settings, module)):
        raise PipelineError(f"Начать с {value}: шаг {module}.{files.pop()} выключен в config.yaml")
    raise PipelineError(f"Начать с {value}: в модуле {module} нет шага с номером {number}")


def resolve_from(settings: Settings, value: str) -> tuple[str, str | None]:
    """С чего начать → (модуль, шаг или None).

    По имени: 'scenario', 'factcheck', 'scenario.factcheck'.
    По номеру папки и файла: '2' — модуль 02_storyboard, '1.3' — шаг 01_scenario/03_youtube_policy.py.
    """
    modules = settings.pipeline
    module, _, step = value.partition(".")
    if module.isdigit():
        module = _module_by_number(settings, module, value)
    if step.isdigit():
        step = _step_by_number(settings, module, step, value)
    if step:
        if module not in modules or step not in module_steps(settings, module):
            raise PipelineError(f"Начать с {value}: такого шага нет или он выключен в config.yaml")
        return module, step
    if module in modules:
        return module, None
    matches = [m for m in modules if module in module_steps(settings, m)]
    if not matches:
        raise PipelineError(f"Начать с {value}: не найден ни модуль, ни включённый шаг с таким именем")
    if len(matches) > 1:
        raise PipelineError(f"Шаг '{value}' есть в модулях {', '.join(matches)} — укажите, например, {matches[0]}.{value}")
    return matches[0], module


def is_done(path: Path) -> bool:
    return path.is_file() or (path.is_dir() and any(path.iterdir()))


def run_pipeline(
    settings: Settings,
    project: Project,
    *,
    from_: str | None = None,
    only: list[str] | None = None,
    force: bool = False,
    provider_factory: ProviderFactory | None = None,
) -> None:
    for name in only or []:
        if name not in settings.pipeline:
            raise PipelineError(f"--only {name}: модуль не включён в pipeline в config.yaml")
    from_module, from_step = resolve_from(settings, from_) if from_ else (None, None)

    # Все шаги загружаются заранее, чтобы опечатка в config.yaml была видна до первого запроса к нейросети.
    modules = [module for module in settings.pipeline if not only or module in only]
    for module in modules:
        check_required(settings, module)
    plan = [(module, step, load_step(module, step)) for module in modules for step in module_steps(settings, module)]
    providers = ProviderPool(settings, provider_factory)

    forced, current = force, None
    for module, step, code in plan:
        if module != current:
            log.info("=== %s ===", module)
            current = module
        if module == from_module and from_step in (None, step):
            forced = True
        output = project.path(module, code.OUTPUT)
        ctx = StepContext(project, settings, module, step, module_steps(settings, module), providers)
        done = code.done(ctx) if hasattr(code, "done") else is_done(output)
        if done and not forced:
            log.info("[%s.%s] пропуск — %s уже есть", module, step, output.relative_to(project.dir).as_posix())
            continue
        log.info("[%s.%s] запуск", module, step)
        try:
            code.run(ctx)
        except NotImplementedError as e:
            raise PipelineError(f"[{module}.{step}] {e or 'ещё не реализован'}") from e
        forced = True
