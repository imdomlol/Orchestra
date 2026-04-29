"""Git worktree management for isolated worker tasks."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Sequence


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

    def create(
        self,
        task_id: str,
        base_ref: str = "main",
        *,
        owned_files: Sequence[str] | None = None,
        forbidden_files: Sequence[str] | None = None,
    ) -> WorktreeInfo:
        self._validate_task_id(task_id)
        branch = f"task/{task_id}"
        path = self.path_for(task_id)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"worktree already exists: {path}")

        self._git("worktree", "add", "-b", branch, str(path), base_ref)
        if owned_files is not None:
            self.install_ownership_hook(
                task_id,
                owned_files=owned_files,
                forbidden_files=forbidden_files or (),
            )
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
        self._remove_ownership_hook(task_id)

    def install_ownership_hook(
        self,
        task_id: str,
        *,
        owned_files: Sequence[str],
        forbidden_files: Sequence[str],
    ) -> Path:
        self._validate_task_id(task_id)
        if not owned_files:
            raise ValueError("owned_files must not be empty")

        path = self.path_for(task_id)
        if not path.exists():
            raise FileNotFoundError(f"worktree does not exist: {path}")

        hooks_path = self._hook_dir(task_id)
        hooks_path.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_path / "pre-commit"
        hook_path.write_text(
            _ownership_hook_source(
                owned_files=tuple(owned_files),
                forbidden_files=tuple(forbidden_files),
            ),
            encoding="utf-8",
        )
        hook_path.chmod(0o755)

        self._git("config", "extensions.worktreeConfig", "true")
        self._git("-C", str(path), "config", "--worktree", "core.hooksPath", str(hooks_path))
        return hook_path

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

    def _hook_dir(self, task_id: str) -> Path:
        return self.repo_root / ".orch" / "hooks" / task_id

    def _validate_task_id(self, task_id: str) -> None:
        if not TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"invalid task id: {task_id}")

    def _remove_ownership_hook(self, task_id: str) -> None:
        hook_dir = self._hook_dir(task_id)
        hook_path = hook_dir / "pre-commit"
        hook_path.unlink(missing_ok=True)
        try:
            hook_dir.rmdir()
        except OSError:
            pass

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )


def _ownership_hook_source(
    *,
    owned_files: tuple[str, ...],
    forbidden_files: tuple[str, ...],
) -> str:
    return f"""#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import subprocess
import sys


OWNED_FILES = {json.dumps(owned_files, indent=2)}
FORBIDDEN_FILES = {json.dumps(forbidden_files, indent=2)}


def staged_paths() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        text=False,
        capture_output=True,
        check=True,
    )
    raw_paths = result.stdout.split(b"\\0")
    return [path.decode("utf-8", errors="replace") for path in raw_paths if path]


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def main() -> int:
    violations = []
    for path in staged_paths():
        if matches(path, FORBIDDEN_FILES):
            violations.append(f"forbidden: {{path}}")
        elif not matches(path, OWNED_FILES):
            violations.append(f"not owned: {{path}}")

    if not violations:
        return 0

    print("orchestra: staged files violate task ownership:", file=sys.stderr)
    for violation in violations:
        print(f"  - {{violation}}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""
