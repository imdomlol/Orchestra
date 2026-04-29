"""Task dispatching from pending YAMLs to worker inbox messages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orch.config import RuntimeConfig
from orch.inbox import Inbox
from orch.task_store import TaskStore
from orch.worktree import WorktreeInfo, WorktreeManager


ACTIVE_DISPATCH_STATUSES = {
    "in_progress",
    "self_review",
    "critic_review",
    "integration_review",
    "ready_to_merge",
}


@dataclass(frozen=True)
class DispatchResult:
    task_id: str
    task_path: Path
    worktree_path: Path
    message_path: Path


class WorktreeCreator(Protocol):
    def create(
        self,
        task_id: str,
        base_ref: str = "main",
        *,
        owned_files: list[str] | None = None,
        forbidden_files: list[str] | None = None,
    ) -> WorktreeInfo:
        ...


class Dispatcher:
    """Move one ready task into active execution and notify the worker."""

    def __init__(
        self,
        *,
        root: Path = Path("."),
        runtime: RuntimeConfig,
        task_store: TaskStore | None = None,
        inbox: Inbox | None = None,
        worktrees: WorktreeCreator | None = None,
        base_ref: str = "main",
    ) -> None:
        self.root = root
        self.runtime = runtime
        self.task_store = task_store or TaskStore(root)
        self.inbox = inbox or Inbox(root)
        self.worktrees = worktrees or WorktreeManager(root)
        self.base_ref = base_ref

    def dispatch_next(self) -> DispatchResult | None:
        """Dispatch the first pending task that is ready to run."""

        if self._active_count() >= self.runtime.max_workers:
            return None

        active_tasks = self._active_tasks()
        for pending_path in self.task_store.list_tasks("pending"):
            task_id = pending_path.stem
            with self.task_store.pickup_lock(task_id):
                if not pending_path.exists():
                    continue
                task = self.task_store.read_path(pending_path)
                if not self._is_ready(task, active_tasks):
                    continue

                info = self.worktrees.create(
                    task_id,
                    self.base_ref,
                    owned_files=task.get("owned_files", []),
                    forbidden_files=task.get("forbidden_files", []),
                )
                active_path = self.task_store.transition(task_id, "active", "in_progress")
                message_path = self.inbox.post(
                    "worker",
                    {
                        "task_id": task_id,
                        "task_yaml_path": str(active_path.relative_to(self.root)),
                        "worktree_path": str(info.path.relative_to(self.root.resolve())),
                        "role": "worker",
                    },
                )
                return DispatchResult(
                    task_id=task_id,
                    task_path=active_path,
                    worktree_path=info.path,
                    message_path=message_path,
                )
        return None

    def _is_ready(self, task: dict[str, Any], active_tasks: list[dict[str, Any]]) -> bool:
        return self._dependencies_merged(task) and not self._collides_with_active(
            task, active_tasks
        )

    def _dependencies_merged(self, task: dict[str, Any]) -> bool:
        for dependency_id in task.get("dependencies", []):
            try:
                dependency = self.task_store.read(dependency_id)
            except FileNotFoundError:
                return False
            if dependency.get("status") != "merged":
                return False
        return True

    def _collides_with_active(
        self, task: dict[str, Any], active_tasks: list[dict[str, Any]]
    ) -> bool:
        owned_files = task.get("owned_files", [])
        for active_task in active_tasks:
            for owned in owned_files:
                for active_owned in active_task.get("owned_files", []):
                    if globs_may_overlap(owned, active_owned):
                        return True
        return False

    def _active_count(self) -> int:
        return len(self._active_tasks())

    def _active_tasks(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for path in self.task_store.list_tasks("active"):
            task = self.task_store.read_path(path)
            if task.get("status") in ACTIVE_DISPATCH_STATUSES:
                tasks.append(task)
        return tasks


def globs_may_overlap(left: str, right: str) -> bool:
    """Conservatively detect overlap between the simple globs used in tasks."""

    if left == right:
        return True

    left_prefix = _static_prefix(left)
    right_prefix = _static_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


def _static_prefix(pattern: str) -> str:
    wildcard_indexes = [
        index for index in (pattern.find("*"), pattern.find("?"), pattern.find("["))
        if index >= 0
    ]
    if not wildcard_indexes:
        return pattern
    return pattern[: min(wildcard_indexes)]
