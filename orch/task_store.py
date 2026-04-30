"""Validated file-backed task storage."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
from pathlib import Path
import shutil
from typing import Any, Iterator

import yaml

from orch.validate_task import validate_task


TASK_DIRS = ("pending", "active", "done")
STATUS_BY_DIR = {
    "pending": {"pending", "planned", "blocked"},
    "active": {
        "in_progress",
        "self_review",
        "critic_review",
        "integration_review",
        "ready_to_merge",
        "blocked",
    },
    "done": {"merged", "abandoned"},
}


@dataclass(frozen=True)
class ReviewNote:
    author: str
    verdict: str
    body: str
    timestamp: str | None = None

    def as_dict(self) -> dict[str, str]:
        timestamp = self.timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return {
            "author": self.author,
            "timestamp": timestamp,
            "verdict": self.verdict,
            "body": self.body,
        }


class TaskStore:
    def __init__(self, root: Path = Path(".")) -> None:
        self.root = root
        self.tasks_root = root / ".orch" / "tasks"
        self.locks_root = root / ".orch" / "locks"

    def read(self, task_id: str) -> dict[str, Any]:
        path = self.path_for(task_id)
        return self.read_path(path)

    def read_path(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        return data

    def write_pending(self, task: dict[str, Any]) -> Path:
        task["status"] = task.get("status", "pending")
        path = self.tasks_root / "pending" / f"{task['id']}.yaml"
        self._write_validated(path, task)
        return path

    def transition(self, task_id: str, target_dir: str, status: str) -> Path:
        if target_dir not in TASK_DIRS:
            raise ValueError(f"invalid task directory: {target_dir}")
        if status not in STATUS_BY_DIR[target_dir]:
            raise ValueError(f"status {status!r} is not allowed in {target_dir}")

        current_path = self.path_for(task_id)
        task = self.read_path(current_path)
        task["status"] = status
        target_path = self.tasks_root / target_dir / current_path.name
        self._write_validated(target_path, task)
        if current_path != target_path:
            current_path.unlink()
        return target_path

    def append_review_note(self, task_id: str, note: ReviewNote) -> Path:
        path = self.path_for(task_id)
        task = self.read_path(path)
        task.setdefault("review_notes", []).append(note.as_dict())
        self._write_validated(path, task)
        return path

    def list_tasks(self, task_dir: str) -> list[Path]:
        if task_dir not in TASK_DIRS:
            raise ValueError(f"invalid task directory: {task_dir}")
        return sorted((self.tasks_root / task_dir).glob("T-*.yaml"))

    def path_for(self, task_id: str) -> Path:
        matches = []
        for task_dir in TASK_DIRS:
            candidate = self.tasks_root / task_dir / f"{task_id}.yaml"
            if candidate.exists():
                matches.append(candidate)
        if not matches:
            raise FileNotFoundError(f"task not found: {task_id}")
        if len(matches) > 1:
            raise RuntimeError(f"task exists in multiple states: {task_id}")
        return matches[0]

    @contextmanager
    def pickup_lock(self, task_id: str) -> Iterator[None]:
        self.locks_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.locks_root / f"{task_id}.lock"
        with lock_path.open("w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def claim_next_pending(self) -> Path | None:
        for path in self.list_tasks("pending"):
            task_id = path.stem
            with self.pickup_lock(task_id):
                if not path.exists():
                    continue
                return self.transition(task_id, "active", "in_progress")
        return None

    def _write_validated(self, path: Path, task: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")
        try:
            validate_task(temp_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        shutil.move(str(temp_path), path)
