"""Папка ролика projects/<имя>/ — по папке на модуль, с теми же именами, что и папки модулей в корне:

  01_scenario/       сценарий; 01_scenario/brief.yaml — тема и параметры ролика
  02_storyboard/     раскадровка
  03_visual/         изображения
  04_audio/          озвучка
  05_montage/        готовый ролик
  old/               прежние результаты после запуска с нуля (--anew)

Папка модуля появляется, когда модуль записывает в неё первый файл.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from core.config import module_dir
from core.llm import LLMResponse

BRIEF_MODULE, BRIEF = "scenario", "brief.yaml"  # тема и параметры лежат в папке сценария
USAGE = "usage.jsonl"
ARCHIVE = "old"  # прежние результаты после запуска с нуля
APPROVED = "approved.json"  # этапы, которые автор проверил в пошаговом режиме
_NAME_RE = re.compile(r"^[\w.-]+$")

log = logging.getLogger(__name__)


class ProjectError(ValueError):
    pass


@dataclass
class Project:
    dir: Path
    topic: str
    params: dict[str, Any] = field(default_factory=dict)  # перекрывают params модулей из config.yaml
    created_at: str = ""

    @property
    def name(self) -> str:
        return self.dir.name

    @classmethod
    def load(cls, dir: Path) -> Project:
        data = yaml.safe_load(brief_path(dir).read_text(encoding="utf-8")) or {}
        return cls(
            dir=dir,
            topic=data.get("topic", ""),
            params=data.get("params") or {},
            created_at=data.get("created_at", ""),
        )

    @classmethod
    def open(
        cls,
        projects_dir: Path,
        name: str,
        topic: str | None = None,
        params: dict[str, Any] | None = None,
        force: bool = False,
    ) -> Project:
        """Открыть существующий ролик или создать новый. Переданные значения сохраняются в brief.yaml.

        Если у готового ролика другая тема, без force — ошибка: новый ролик лучше делать в новой папке.
        """
        if not _NAME_RE.match(name):
            raise ProjectError(f"Имя проекта '{name}': только буквы, цифры, '-', '_' и '.'")
        dir = projects_dir / name
        if brief_path(dir).exists():
            project = cls.load(dir)
            if topic and topic != project.topic:
                if not force:
                    raise ProjectError(
                        f"Ролик '{name}' уже сделан на тему «{project.topic}», а в config.yaml тема «{topic}». "
                        f"Для нового ролика укажите другое имя: python run.py --project <новое_имя>. "
                        f"Переделать '{name}' на новую тему с нуля: добавьте --anew"
                    )
                log.info("Тема ролика изменена на «%s» — всё пересчитывается заново", topic)
                project.topic = topic
            project.params = {**project.params, **(params or {})}
        else:
            if not topic:
                raise ProjectError("Не задана тема ролика — впишите её в config.yaml: topic: ...")
            project = cls(
                dir=dir,
                topic=topic,
                params=params or {},
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
        project.save()
        return project

    def save(self) -> None:
        path = brief_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"topic": self.topic, "params": self.params, "created_at": self.created_at}
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def archive(self) -> Path | None:
        """Запуск с нуля: все результаты → old/<дата_время>/, ничего не удаляется. Тема и параметры (brief.yaml) остаются."""
        target = self.dir / ARCHIVE / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        moved = False
        (self.dir / APPROVED).unlink(missing_ok=True)
        for folder in sorted(self.dir.iterdir()):
            if not folder.is_dir() or folder.name == ARCHIVE:
                continue
            for item in sorted(folder.iterdir()):
                if item == brief_path(self.dir):
                    continue
                (target / folder.name).mkdir(parents=True, exist_ok=True)
                try:
                    item.rename(target / folder.name / item.name)
                except OSError as e:
                    raise ProjectError(f"Не удалось перенести {item} в {target}: {e}. Закройте этот файл, если он открыт, и запустите ещё раз") from e
                moved = True
        return target if moved else None

    def approved(self) -> set[str]:
        path = self.dir / APPROVED
        return set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()

    def set_approved(self, module: str, approved: bool) -> None:
        modules = self.approved()
        if (module in modules) != approved:
            modules = modules | {module} if approved else modules - {module}
            (self.dir / APPROVED).write_text(json.dumps(sorted(modules)), encoding="utf-8")

    def path(self, module: str, *parts: str) -> Path:
        """Файл в папке модуля: path("scenario", "script.md") → projects/<имя>/01_scenario/script.md."""
        return self.dir.joinpath(module_dir(module).name, *parts)

    def log_usage(self, module: str, step: str, provider: str, response: LLMResponse) -> None:
        """Каждый запрос к нейросети — строка в usage.jsonl папки модуля: что, где и сколько токенов."""
        self.log(module, {
            "step": step,
            "provider": provider,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        })

    def log(self, module: str, record: dict[str, Any]) -> None:
        """Строка в usage.jsonl папки модуля — по ней видно, сколько запросов и куда ушло."""
        record = {"time": datetime.now().isoformat(timespec="seconds"), **record}
        path = self.path(module, USAGE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock, path.open("a", encoding="utf-8") as f:  # картинки генерируются и проверяются в несколько потоков
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


_log_lock = threading.Lock()


def brief_path(dir: Path) -> Path:
    return dir / module_dir(BRIEF_MODULE).name / BRIEF
