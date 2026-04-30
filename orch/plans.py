"""Planner artifact ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from scripts.validate_task import validate_task
from orch.task_store import TaskStore


TASK_BLOCK_RE = re.compile(r"```(?:yaml|yml)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class PlanIngestResult:
    plan_path: Path
    task_paths: tuple[Path, ...]

    @property
    def task_count(self) -> int:
        return len(self.task_paths)


class PlanBudgetExceeded(ValueError):
    """Raised when a plan would exceed configured ingestion budgets."""

    def __init__(self, *, task_count: int, max_tasks: int) -> None:
        self.task_count = task_count
        self.max_tasks = max_tasks
        super().__init__(
            f"plan contains {task_count} tasks, exceeding max_tasks_per_request={max_tasks}"
        )


class PlanIngestor:
    """Convert embedded planner task YAML blocks into pending task files."""

    def __init__(self, root: Path = Path("."), *, task_store: TaskStore | None = None) -> None:
        self.root = root.resolve()
        self.task_store = task_store or TaskStore(self.root)

    def ingest(self, plan_path: Path | str, *, max_tasks: int | None = None) -> PlanIngestResult:
        resolved_plan = self._resolve_inside_root(plan_path)
        content = resolved_plan.read_text(encoding="utf-8")
        tasks = extract_task_blocks(content)
        if not tasks:
            raise ValueError(f"plan contains no task YAML blocks: {resolved_plan}")
        if max_tasks is not None and len(tasks) > max_tasks:
            raise PlanBudgetExceeded(task_count=len(tasks), max_tasks=max_tasks)

        seen_ids: set[str] = set()
        for task in tasks:
            task_id = task["id"]
            if task_id in seen_ids:
                raise ValueError(f"plan contains duplicate task id: {task_id}")
            seen_ids.add(task_id)
            try:
                self.task_store.path_for(task_id)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"task already exists: {task_id}")
        self._validate_tasks(tasks)

        task_paths = []
        for task in tasks:
            task_paths.append(self.task_store.write_pending(task))

        return PlanIngestResult(plan_path=resolved_plan, task_paths=tuple(task_paths))

    def _resolve_inside_root(self, plan_path: Path | str) -> Path:
        path = Path(plan_path)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"plan path is outside repo root: {plan_path}") from exc
        return path

    def _validate_tasks(self, tasks: list[dict[str, Any]]) -> None:
        scratch = self.root / ".orch" / "plans" / ".validate"
        scratch.mkdir(parents=True, exist_ok=True)
        schema_path = self.root / ".orch" / "schemas" / "task.schema.json"
        for task in tasks:
            temp_path = scratch / f"{task['id']}.yaml"
            temp_path.write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")
            try:
                validate_task(temp_path, schema_path)
            finally:
                temp_path.unlink(missing_ok=True)
        try:
            scratch.rmdir()
        except OSError:
            pass


def extract_task_blocks(markdown: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for match in TASK_BLOCK_RE.finditer(markdown):
        data = yaml.safe_load(match.group(1))
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("id"), str) and data["id"].startswith("T-"):
            tasks.append(data)
    return tasks
