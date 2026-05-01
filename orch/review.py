"""Review handoff helpers for completed worker tasks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from orch.inbox import Inbox
from orch.task_store import TaskStore

CRITIC_MODES = {"opus", "gemini", "both"}
CRITIC_OVERRIDES = {"gemini", "both"}


@dataclass(frozen=True)
class CriticDispatchResult:
    task_id: str
    task_path: Path
    diff_path: Path
    message_path: Path
    mode: str = "gemini"


@dataclass(frozen=True)
class DiffExportResult:
    task_id: str
    task_path: Path
    diff_path: Path
    contents: str


class ReviewDispatcher:
    """Prepare worker output for critic review."""

    def __init__(
        self,
        root: Path = Path("."),
        *,
        task_store: TaskStore | None = None,
        inbox: Inbox | None = None,
        base_ref: str = "main",
    ) -> None:
        self.root = root.resolve()
        self.task_store = task_store or TaskStore(self.root)
        self.inbox = inbox or Inbox(self.root)
        self.base_ref = base_ref
        self.patches_root = self.root / ".orch" / "patches"

    def dispatch_to_critic(self, task_id: str) -> CriticDispatchResult:
        task = self.task_store.read(task_id)
        branch = task["branch"]
        diff_path = self._export_diff(task_id, branch, force=True).diff_path
        task_path = self.task_store.transition(task_id, "active", "critic_review")
        message_path = self.inbox.post(
            "critic",
            {
                "task_id": task_id,
                "task_yaml_path": str(task_path.relative_to(self.root)),
                "diff_path": str(diff_path.relative_to(self.root)),
                "policy_path": ".orch/config/policies.toml",
                "role": "critic",
            },
        )
        return CriticDispatchResult(
            task_id=task_id,
            task_path=task_path,
            diff_path=diff_path,
            message_path=message_path,
            mode="gemini",
        )

    def dispatch_to_critic_for_opus(self, task_id: str) -> CriticDispatchResult:
        task = self.task_store.read(task_id)
        branch = task["branch"]
        diff_path = self._export_diff(task_id, branch, force=True).diff_path
        task_path = self.task_store.transition(task_id, "active", "self_review")
        message_path = self.inbox.post(
            "critic",
            {
                "task_id": task_id,
                "task_yaml_path": str(task_path.relative_to(self.root)),
                "diff_path": str(diff_path.relative_to(self.root)),
                "policy_path": ".orch/config/policies.toml",
                "role": "critic",
                "critic_mode": "both",
                "final_reviewer": "opus",
            },
        )
        return CriticDispatchResult(
            task_id=task_id,
            task_path=task_path,
            diff_path=diff_path,
            message_path=message_path,
            mode="both",
        )

    def export_diff(self, task_id: str) -> DiffExportResult:
        task = self.task_store.read(task_id)
        return self._export_diff(task_id, task["branch"])

    def _export_diff(
        self,
        task_id: str,
        branch: str,
        *,
        force: bool = False,
    ) -> DiffExportResult:
        self.patches_root.mkdir(parents=True, exist_ok=True)
        diff_path = self.patches_root / f"{task_id}.diff"
        if diff_path.exists() and not force:
            return DiffExportResult(
                task_id=task_id,
                task_path=self.task_store.path_for(task_id),
                diff_path=diff_path,
                contents=diff_path.read_text(encoding="utf-8"),
            )
        result = subprocess.run(
            ["git", "diff", f"{self.base_ref}..{branch}"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"failed to export review diff for {task_id}: {message}")
        diff_path.write_text(result.stdout, encoding="utf-8")
        return DiffExportResult(
            task_id=task_id,
            task_path=self.task_store.path_for(task_id),
            diff_path=diff_path,
            contents=result.stdout,
        )


def resolve_critic_mode(task: dict, default_mode: str) -> str:
    if default_mode not in CRITIC_MODES:
        allowed = ", ".join(sorted(CRITIC_MODES))
        raise ValueError(f"critic.mode must be one of: {allowed}")

    override = task.get("critic_override")
    if override is None:
        return default_mode
    if override not in CRITIC_OVERRIDES:
        allowed = ", ".join(sorted(CRITIC_OVERRIDES))
        raise ValueError(f"critic_override must be one of: {allowed}")
    return override
