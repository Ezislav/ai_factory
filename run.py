"""Точка входа ai-factory.

  python run.py --project my_video                                   # ролик на тему из config.yaml (topic: ...);
                                                                     # повторный запуск продолжает: готовые шаги пропускаются
  python run.py --project my_video 2                                 # начать с модуля 02_storyboard и пересчитать всё после
  python run.py --project my_video 1.3                               # начать с шага 01_scenario/03_youtube_policy
  python run.py --project my_video factcheck                         # то же по имени: scenario, factcheck, scenario.revise
  python run.py --list                                               # шаги, нейросети и генераторы

Библиотеки живут в core/.venv: при первом запуске run.py сам создаёт её и ставит всё из REQUIREMENTS,
дальше просто перезапускается внутри неё. Чтобы переустановить библиотеки, удалите core/.venv.

Настройки — в config.yaml, ключи — в .env: ANTHROPIC_API_KEY=..., OPENAI_API_KEY=..., GOOGLE_API_KEY=..., DEEPSEEK_API_KEY=...
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True  # без папок __pycache__ в core/ и папках модулей

ROOT = Path(__file__).resolve().parent
VENV = ROOT / "core" / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
VENV_READY = VENV / ".installed"  # список установленных библиотек; изменился REQUIREMENTS — доустанавливаем
REQUIREMENTS = [
    "anthropic>=2.3,<3",
    "openai>=3.41,<4",
    "pydantic>=2.14,<3",
    "pyyaml>=6.0",
    "jinja2>=3.1",
    "python-dotenv>=1.2",
    "google-genai>=2.34,<3",
]


def ensure_venv() -> None:
    """Перезапустить run.py внутри core/.venv, при первом запуске — создать её."""
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    wanted = "\n".join(REQUIREMENTS)
    if not VENV_PYTHON.exists():
        print("Первый запуск: создаю core/.venv и ставлю библиотеки (около минуты)...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        if os.name == "nt":
            subprocess.run(["attrib", "+h", str(VENV)], check=False)  # скрыть в Проводнике
    if not VENV_READY.exists() or VENV_READY.read_text(encoding="utf-8") != wanted:
        print("Ставлю библиотеки в core/.venv...", flush=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-q", "--disable-pip-version-check", *REQUIREMENTS], check=True)
        VENV_READY.write_text(wanted, encoding="utf-8")
    # Ctrl+C получают оба процесса. Внутренний останавливается сам и сохраняет готовое — ждём его.
    # Второй Ctrl+C — прервать сразу.
    process = subprocess.Popen([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
    stopping = False
    while True:
        try:
            sys.exit(process.wait(timeout=0.5))
        except subprocess.TimeoutExpired:
            continue
        except KeyboardInterrupt:
            if stopping:
                process.kill()
                sys.exit(130)
            stopping = True


if __name__ == "__main__":
    ensure_venv()

import yaml

from core.config import PROJECTS_DIR, ConfigError, bare_name, load_settings, module_dir
from core.llm import LLMError
from core.media import MediaError
from core.pipeline import PipelineError, declared_steps, run_pipeline
from core.project import Project, ProjectError
from core.prompts import PromptError
from core.structured import StructuredOutputError


def parse_set(values: list[str]) -> dict[str, Any]:
    """--set key=value; значение разбирается как YAML, поэтому числа остаются числами."""
    params = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--set ожидает key=value, получено: {item}")
        params[key.strip()] = yaml.safe_load(value)
    return params


def print_catalog() -> None:
    settings = load_settings()
    print("Шаги модулей (modules → <модуль> → steps в config.yaml; [x] — включён):")
    for module in settings.pipeline:
        steps = "  ".join(f"[{'x' if on else ' '}] {step}" for step, on in declared_steps(settings, module).items())
        print(f"  {module_dir(module).name:<14} {steps}")
    print("Нейросети (llm.providers в config.yaml):")
    for name, config in (settings.llm.get("providers") or {}).items():
        mark = "*" if name == settings.llm.get("default") else " "
        print(f"{mark} {name:<12} {config.get('type'):<10} {config.get('model', '')}")
    print("Генераторы картинок и видео (media.providers в config.yaml):")
    for name, config in (settings.media.get("providers") or {}).items():
        mark = "*" if name in (settings.media.get("default_image"), settings.media.get("default_video")) else " "
        print(f"{mark} {name:<12} {config.get('type'):<12} {config.get('model', '')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ai-factory: генерация YouTube-роликов")
    parser.add_argument("start", nargs="?", metavar="ЭТАП",
                        help="начать с этапа и пересчитать всё после него: 2, 1.3, storyboard, factcheck")
    parser.add_argument("--project", help="имя папки в projects/")
    parser.add_argument("--topic", help="тема ролика вместо topic из config.yaml")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="параметр для промптов, сохраняется в brief.yaml ролика; можно несколько раз")
    parser.add_argument("--provider", help="одна нейросеть из llm.providers на весь запуск")
    parser.add_argument("--from", dest="from_", metavar="STEP",
                        help="то же, что ЭТАП")
    parser.add_argument("--only", help="запустить только эти модули, через запятую")
    parser.add_argument("--force", action="store_true", help="пересчитать всё заново")
    parser.add_argument("--list", action="store_true", help="показать шаги, нейросети и генераторы")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.list:
            print_catalog()
            return 0
        if not args.project:
            parser.error("укажите --project")
        if args.start and args.from_:
            parser.error("укажите этап один раз: либо 2, либо --from 2")
        settings = load_settings()  # проверить config.yaml до создания папки ролика
        topic = args.topic or str(settings.data.get("topic") or "").strip() or None
        project = Project.open(PROJECTS_DIR, args.project, topic=topic, params=parse_set(args.set), force=args.force)
        settings.provider_override = args.provider
        only = [bare_name(m.strip()) for m in args.only.split(",")] if args.only else None
        run_pipeline(settings, project, from_=args.start or args.from_, only=only, force=args.force)
    except (ConfigError, LLMError, MediaError, PipelineError, ProjectError, PromptError, StructuredOutputError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nОстановлено (Ctrl+C). Готовые файлы сохранены — следующий запуск продолжит с места остановки.", file=sys.stderr)
        return 130

    print(f"\nГотово: {project.dir}")
    for label, path in [("Сценарий", project.path("scenario", "script.md")),
                        ("Раскадровка", project.path("storyboard", "storyboard.md")),
                        ("Картинки", project.path("images", "images.json")),
                        ("Проверка картинок", project.path("images", "review.html"))]:
        if path.exists():
            print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
