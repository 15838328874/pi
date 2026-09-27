"""Load eval tasks from JSON files (a task set = a directory of *.json)."""

from __future__ import annotations

from pathlib import Path

from pi.evals.schema import Task


def load_task(path: str | Path) -> Task:
    p = Path(path)
    return Task.model_validate_json(p.read_text(encoding="utf-8"))


def load_task_set(path: str | Path) -> list[Task]:
    p = Path(path)
    if p.is_dir():
        return [
            Task.model_validate_json(f.read_text(encoding="utf-8"))
            for f in sorted(p.glob("*.json"))
        ]
    return [load_task(p)]
