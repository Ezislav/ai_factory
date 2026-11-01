"""Точка входа ai-factory.

  python run.py --project my_video --continue    # продолжить с первого незаконченного этапа, готовое не трогать (то же без ключа)
  python run.py --project my_video --only 4,5    # только эти этапы
  python run.py --project my_video --2           # с этапа 02_storyboard и до конца
  python run.py --project my_video --1.3         # с шага 01_scenario/03_youtube_policy и до конца
  python run.py --project my_video --anew        # с нуля и до конца; прежние результаты уйдут в old/
  python run.py --project my_video --step        # по этапам: после каждого проверка, правки в redo.txt, Enter — далее
  python run.py --list                           # шаги, нейросети и генераторы

Подробнее — в README.md.

Библиотеки живут в core/.venv: при первом запуске run.py сам создаёт её и ставит всё из REQUIREMENTS,
дальше просто перезапускается внутри неё. Чтобы переустановить библиотеки, удалите core/.venv.

Настройки — в config.yaml, ключи — в .env (образец — .env.example).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
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

from core.config import PROJECTS_DIR, ConfigError, load_settings, module_dir
from core.llm import LLMError
from core.media import MediaError
from core.pipeline import PipelineError, PipelineStopped, declared_steps, run_pipeline
from core.project import Project, ProjectError
from core.prompts import PromptError
from core.structured import StructuredOutputError

START = re.compile(r"^--(\d+(?:\.\d+)?)$")  # --2, --1.3


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


def split_start(argv: list[str]) -> tuple[list[str], list[str]]:
    """--2 и --1.3 argparse не понимает: номера этапов разбираются отдельно. → (остальные аргументы, номера)."""
    rest, starts = [], []
    for item in argv:
        match = START.match(item)
        if match:
            starts.append(match.group(1))
        else:
            rest.append(item)
    return rest, starts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ai-factory: генерация YouTube-роликов",
        epilog="--2 — начать с этапа 2 и идти до конца, --1.3 — начать с шага 3 этапа 1. Все команды — в README.md.",
    )
    parser.add_argument("--project", help="имя папки в projects/")
    parser.add_argument("--topic", help="тема ролика вместо topic из config.yaml")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="параметр для промптов, сохраняется в brief.yaml ролика; можно несколько раз")
    parser.add_argument("--provider", help="одна нейросеть из llm.providers на весь запуск")
    parser.add_argument("--continue", dest="resume", action="store_true",
                        help="продолжить с первого незаконченного этапа; то же, что запуск без ключа")
    parser.add_argument("--only", metavar="ЭТАПЫ", help="только эти этапы через запятую: 4 или 4,5")
    parser.add_argument("--anew", action="store_true", help="с нуля и до конца; прежние результаты уйдут в old/ ролика")
    parser.add_argument("--step", action="store_true", help="по этапам: после каждого ждать проверки, правки — в redo.txt этапа")
    parser.add_argument("--list", action="store_true", help="показать шаги, нейросети и генераторы")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv, starts = split_start(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.list:
            print_catalog()
            return 0
        if not args.project:
            parser.error("укажите --project")
        if len(starts) + bool(args.only) + args.anew + args.resume > 1:
            parser.error("выберите что-то одно: --continue, --only, --anew или номер этапа (--2)")
        settings = load_settings()  # проверить config.yaml до создания папки ролика
        topic = args.topic or str(settings.data.get("topic") or "").strip() or None
        project = Project.open(PROJECTS_DIR, args.project, topic=topic, params=parse_set(args.set), force=args.anew)
        settings.provider_override = args.provider
        settings.step_by_step = args.step
        only = [m.strip() for m in args.only.split(",") if m.strip()] if args.only else None
        run_pipeline(settings, project, from_=starts[0] if starts else None, only=only, anew=args.anew)
    except (ConfigError, LLMError, MediaError, PipelineError, ProjectError, PromptError, StructuredOutputError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except PipelineStopped:
        print(f"\nОстановлено. Продолжить проверку: python run.py --project {args.project} --step")
        return 0
    except KeyboardInterrupt:
        print("\nОстановлено (Ctrl+C). Готовые файлы сохранены — следующий запуск продолжит с места остановки.", file=sys.stderr)
        return 130

    print(f"\nГотово: {project.dir}")
    for label, path in [("Сценарий", project.path("scenario", "script.md")),
                        ("Раскадровка", project.path("storyboard", "storyboard.md")),
                        ("Картинки", project.path("visual", "images.json")),
                        ("Проверка картинок", project.path("visual", "review.html")),
                        ("Озвучка", project.path("audio", "voice.wav")),
                        ("Проект FilmCraft", project.path("montage", "video.fcproj")),
                        ("Ролик", project.path("montage", "video.mp4"))]:
        if path.exists():
            print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
