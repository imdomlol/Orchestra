"""Patch-based task integration for active worker branches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Sequence

from orch.config import OrchestraConfig, load_config
from orch.runner import ProcessResult, SubprocessRunner, make_runner
from orch.task_store import ReviewNote, TaskStore


@dataclass(frozen=True)
class MergeResult:
    task_id: str
    status: str
    patch_path: Path
    integration_path: Path
    check_results: tuple[ProcessResult, ...] = ()
    message: str = ""

    @property
    def merged(self) -> bool:
        return self.status == "merged"


class MergeDriver:
    """Apply a task branch through an integration worktree, then merge to main."""

    def __init__(
        self,
        *,
        root: Path = Path("."),
        task_store: TaskStore | None = None,
        runner: SubprocessRunner | None = None,
        check_commands: Sequence[str] = (),
        check_timeout_seconds: int = 1800,
        base_ref: str = "main",
    ) -> None:
        self.root = root.resolve()
        self.task_store = task_store or TaskStore(self.root)
        self.runner = runner or SubprocessRunner(self.root)
        self.check_commands = tuple(check_commands)
        self.check_timeout_seconds = check_timeout_seconds
        self.base_ref = base_ref
        self.worktrees_root = self.root / ".orch" / "worktrees"
        self.patches_root = self.root / ".orch" / "patches"

    @classmethod
    def from_config(
        cls,
        *,
        root: Path = Path("."),
        config: OrchestraConfig | None = None,
        check_commands: Sequence[str] = (),
        base_ref: str = "main",
    ) -> "MergeDriver":
        resolved_root = root.resolve()
        loaded = config or load_config(resolved_root / ".orch" / "config")
        return cls(
            root=resolved_root,
            runner=make_runner(resolved_root, sandbox=loaded.sandbox),
            check_commands=check_commands,
            check_timeout_seconds=loaded.runtime.default_timeout_seconds,
            base_ref=base_ref,
        )

    def merge_task(self, task_id: str) -> MergeResult:
        task = self.task_store.read(task_id)
        branch = task["branch"]
        objective = task["objective"]
        patch_path = self._export_patch(task_id, branch)
        integration_path = self.worktrees_root / "_integration"
        integrate_branch = f"integrate/{task_id}"

        self._prepare_integration_worktree(integration_path, integrate_branch)
        try:
            am = self._git(
                "-C",
                str(integration_path),
                "am",
                "--3way",
                str(patch_path),
                check=False,
            )
            if am.returncode != 0:
                self._git("-C", str(integration_path), "am", "--abort", check=False)
                message = _stderr_or_stdout(am)
                self._note(task_id, "request_changes", f"Patch did not apply:\n{message}")
                self._cleanup_integration(integration_path, integrate_branch)
                return MergeResult(
                    task_id=task_id,
                    status="conflict",
                    patch_path=patch_path,
                    integration_path=integration_path,
                    message=message,
                )

            check_results = self._run_checks(task_id, integration_path)
            failed = next((result for result in check_results if not result.succeeded), None)
            if failed is not None:
                message = f"Integration check failed: {' '.join(failed.argv)}"
                self._note(task_id, "request_changes", message)
                self._cleanup_integration(integration_path, integrate_branch)
                return MergeResult(
                    task_id=task_id,
                    status="checks_failed",
                    patch_path=patch_path,
                    integration_path=integration_path,
                    check_results=check_results,
                    message=message,
                )

            self._git("-C", str(self.root), "checkout", self.base_ref)
            self._git(
                "-C",
                str(self.root),
                "merge",
                "--no-ff",
                "-m",
                f"merge({task_id}): {objective}",
                integrate_branch,
            )
            self.task_store.transition(task_id, "done", "merged")
            self._cleanup_integration(integration_path, integrate_branch)
            worker_path = self.root / task["worktree_path"]
            self._remove_worktree(worker_path)
            self._delete_branch(branch)
            return MergeResult(
                task_id=task_id,
                status="merged",
                patch_path=patch_path,
                integration_path=integration_path,
                check_results=check_results,
                message="merged",
            )
        except Exception:
            if integration_path.exists():
                self._git("-C", str(integration_path), "am", "--abort", check=False)
            raise

    def _export_patch(self, task_id: str, branch: str) -> Path:
        self.patches_root.mkdir(parents=True, exist_ok=True)
        patch_path = self.patches_root / f"{task_id}.patch"
        result = self._git(
            "format-patch",
            f"{self.base_ref}..{branch}",
            "--stdout",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to export patch for {task_id}: {_stderr_or_stdout(result)}")
        patch_path.write_text(result.stdout, encoding="utf-8")
        return patch_path

    def _prepare_integration_worktree(self, path: Path, branch: str) -> None:
        if path.exists():
            raise FileExistsError(f"integration worktree already exists: {path}")
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        self._delete_branch(branch)
        self._git("worktree", "add", "--detach", str(path), self.base_ref)
        self._git("-C", str(path), "checkout", "-b", branch)

    def _run_checks(self, task_id: str, cwd: Path) -> tuple[ProcessResult, ...]:
        results: list[ProcessResult] = []
        for index, command in enumerate(self.check_commands, start=1):
            result = self.runner.run_allowed(
                command,
                allowed_commands=self.check_commands,
                role="integrator",
                log_name=f"{task_id}-check-{index}",
                cwd=cwd,
                timeout_seconds=self.check_timeout_seconds,
            )
            results.append(result)
            if not result.succeeded:
                break
        return tuple(results)

    def _note(self, task_id: str, verdict: str, body: str) -> None:
        self.task_store.append_review_note(
            task_id,
            ReviewNote(author="codex-integrator", verdict=verdict, body=body),
        )

    def _remove_worktree(self, path: Path) -> None:
        if path.exists():
            self._git("worktree", "remove", str(path))
        shutil.rmtree(path, ignore_errors=True)

    def _cleanup_integration(self, path: Path, branch: str) -> None:
        self._remove_worktree(path)
        self._delete_branch(branch)

    def _delete_branch(self, branch: str) -> None:
        self._git("branch", "-D", branch, check=False)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=check,
        )


def _stderr_or_stdout(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout).strip()
