"""Git worktree management for isolated worker tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


TASK_ID_RE = re.compile(r"^T-[0-9]{4}(-[a-z0-9-]+)?$")


@dataclass(frozen=True)
class WorktreeInfo:
    task_id: str
    branch: str
    path: Path


class WorktreeManager:
    def __init__(self, repo_root: Path = Path(".")) -> None:
        self.repo_root = repo_root.resolve()
        self.worktrees_root = self.repo_root / ".orch" / "worktrees"

    def create(self, task_id: str, base_ref: str = "main") -> WorktreeInfo:
        self._validate_task_id(task_id)
        branch = f"task/{task_id}"
        path = self.path_for(task_id)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"worktree already exists: {path}")

        self._git("worktree", "add", "-b", branch, str(path), base_ref)
        return WorktreeInfo(task_id=task_id, branch=branch, path=path)

    def remove(self, task_id: str, base_ref: str = "main") -> None:
        self._validate_task_id(task_id)
        path = self.path_for(task_id)
        branch = f"task/{task_id}"
        if not path.exists():
            raise FileNotFoundError(f"worktree does not exist: {path}")
        if self.is_dirty(path):
            raise RuntimeError(f"refusing to remove dirty worktree: {path}")
        if self.has_unmerged_commits(branch, base_ref):
            raise RuntimeError(f"refusing to remove unmerged branch: {branch}")

        self._git("worktree", "remove", str(path))
        if self.branch_exists(branch):
            self._git("branch", "-D", branch)

    def path_for(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        path = (self.worktrees_root / task_id).resolve()
        try:
            path.relative_to(self.worktrees_root.resolve())
        except ValueError as exc:
            raise ValueError(f"unsafe worktree path: {path}") from exc
        return path

    def is_dirty(self, worktree_path: Path) -> bool:
        output = self._git("-C", str(worktree_path), "status", "--porcelain")
        return bool(output.stdout.strip())

    def has_unmerged_commits(self, branch: str, base_ref: str = "main") -> bool:
        output = self._git("rev-list", "--count", f"{base_ref}..{branch}")
        return int(output.stdout.strip()) > 0

    def branch_exists(self, branch: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def _validate_task_id(self, task_id: str) -> None:
        if not TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"invalid task id: {task_id}")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
