"""Запуск пайплайна: модули по порядку из config.yaml, внутри модуля — шаги по порядку.

Модуль — папка в корне проекта с номером или без: 01_scenario/ или scenario/. Шаги — файлы в ней
(см. core/step.py), их порядок и переключатели — modules → <NN_модуль> → steps в config.yaml ({шаг: true/false}).
Шаг пропускается, если его OUTPUT уже есть. После пересчёта любого шага пересчитываются все следующие — в этом модуле и в следующих.
В пошаговом режиме (--step) после каждого модуля программа ждёт автора: правки из redo.txt применяются, Enter — дальше.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

from core.config import PROJECT_ROOT, Settings, bare_name, module_dir, step_file
from core.project import Project
from core.step import OLD, REDO, PipelineError, ProviderFactory, ProviderPool, StepContext

__all__ = ["PipelineError", "PipelineStopped", "declared_steps", "load_step", "module_dir", "module_steps", "resolve_from", "resolve_only", "run_pipeline"]

log = logging.getLogger(__name__)

_loaded: dict[tuple[str, str], ModuleType] = {}
_NUMBERED = re.compile(r"^(\d+)[._ -]*(.+)$")  # 02_storyboard → 02, storyboard


class PipelineStopped(Exception):
    """Автор остановил пошаговый режим."""


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
        raise PipelineError(f"Этап {value}: нет папки модуля с номером {number}")
    name, path = folders[0]
    if name not in settings.pipeline:
        raise PipelineError(
            f"Этап {value}: модуль {path.name} выключен — добавьте {path.name} в pipeline в config.yaml"
        )
    return name


def resolve_only(settings: Settings, value: str) -> str:
    """Этап для --only: по номеру папки (4) или по имени (audio, 04_audio)."""
    name = _module_by_number(settings, value, value) if value.isdigit() else bare_name(value)
    if name not in settings.pipeline:
        raise PipelineError(f"--only {value}: такого этапа нет в pipeline в config.yaml")
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
    anew: bool = False,
    provider_factory: ProviderFactory | None = None,
) -> None:
    """Без аргументов — продолжить: готовые шаги пропускаются. from_ — с этого места и до конца,
    only — только эти модули, anew — с нуля: прежние результаты уходят в old/ ролика."""
    only = [resolve_only(settings, name) for name in only or []]
    from_module, from_step = resolve_from(settings, from_) if from_ else (None, None)

    # Все шаги загружаются заранее, чтобы опечатка в config.yaml была видна до первого запроса к нейросети.
    modules = [module for module in settings.pipeline if not only or module in only]
    for module in modules:
        check_required(settings, module)
    plan = {module: [(step, load_step(module, step)) for step in module_steps(settings, module)] for module in modules}
    providers = ProviderPool(settings, provider_factory)

    if anew:
        archive = project.archive()
        if archive:
            log.info("Прежние результаты перенесены в %s", archive)

    forced = anew or bool(only)
    waiting = from_module is not None  # до этапа, с которого просили начать, ничего не запускается
    for number, (module, steps) in enumerate(plan.items(), 1):
        if waiting and module != from_module:
            log.info("=== %s === пропуск — начинаем с %s", module, from_)
            continue
        log.info("=== %s ===", module)
        while True:
            for step, code in steps:
                if waiting and from_step not in (None, step):
                    continue
                if waiting:
                    waiting, forced = False, True
                output = project.path(module, code.OUTPUT)
                ctx = StepContext(project, settings, module, step, module_steps(settings, module), providers)
                if not forced and (code.done(ctx) if hasattr(code, "done") else is_done(output)):
                    log.info("[%s.%s] пропуск — %s уже есть", module, step, output.relative_to(project.dir).as_posix())
                    continue
                log.info("[%s.%s] запуск", module, step)
                try:
                    code.run(ctx)
                except NotImplementedError as e:
                    raise PipelineError(f"[{module}.{step}] {e or 'ещё не реализован'}") from e
                forced = True
                project.set_approved(module, False)
            ensure_redo(ctx, steps)
            if not settings.step_by_step or module in project.approved():
                break
            ask_author(ctx, steps, number, len(plan))
            if not ctx.redo_lines():
                project.set_approved(module, True)
                break
            # Правки: заново только то, что они затрагивают (шаги сами видят непустой redo.txt), и всё после этого.
            forced = False


def ensure_redo(ctx: StepContext, steps: list[tuple[str, ModuleType]]) -> None:
    """Файл для правок появляется вместе с результатом модуля."""
    help_text = next((code.REDO_HELP for _, code in steps if hasattr(code, "REDO_HELP")), None)
    if help_text and not ctx.path(REDO).exists():
        ctx.clear_redo(help_text)


def ask_author(ctx: StepContext, steps: list[tuple[str, ModuleType]], number: int, total: int) -> None:
    """Пауза пошагового режима. Enter — дальше; если в redo.txt есть правки, сначала применятся они."""
    code = steps[-1][1]
    print(f"\n=== Этап {number} из {total} готов: {module_dir(ctx.module).name} ===")
    print(f"Проверьте:  {ctx.path(getattr(code, 'SHOW', code.OUTPUT))}")
    print(f"Правки:     {ctx.path(REDO)}  (впишите и сохраните файл)")
    print(f"Прежние варианты — в папке {OLD} рядом с результатом.")
    try:
        answer = input("Enter — далее (правки из redo.txt применятся сами), q — остановиться: ")
    except EOFError:
        raise PipelineStopped from None
    if answer.strip().lower() in ("q", "й"):
        raise PipelineStopped
