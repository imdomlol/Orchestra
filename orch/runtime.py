"""Runtime loop primitives for the local orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4

from orch.config import OrchestraConfig, RuntimeConfig, load_config
from orch.dispatcher import DispatchResult, Dispatcher
from orch.inbox import Inbox, InboxMessage
from orch.task_store import TaskStore


@dataclass(frozen=True)
class SubmitResult:
    request_path: Path
    message_path: Path


@dataclass(frozen=True)
class ReconcileResult:
    active_tasks: int
    orchestrator_messages: int
    worktrees: int
    cleared_stale_pid: bool


@dataclass(frozen=True)
class RunOnceResult:
    kind: str
    message: str
    dispatch: DispatchResult | None = None
    inbox_message: InboxMessage | None = None


class OrchestraRuntime:
    """Small deterministic runtime loop over inbox and dispatch state."""

    def __init__(
        self,
        *,
        root: Path = Path("."),
        runtime_config: RuntimeConfig,
        task_store: TaskStore | None = None,
        inbox: Inbox | None = None,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self.root = root.resolve()
        self.runtime_config = runtime_config
        self.task_store = task_store or TaskStore(self.root)
        self.inbox = inbox or Inbox(self.root)
        self.dispatcher = dispatcher or Dispatcher(
            root=self.root,
            runtime=runtime_config,
            task_store=self.task_store,
            inbox=self.inbox,
        )

    @classmethod
    def from_config(
        cls,
        *,
        root: Path = Path("."),
        config: OrchestraConfig | None = None,
    ) -> "OrchestraRuntime":
        resolved_root = root.resolve()
        loaded = config or load_config(resolved_root / ".orch" / "config")
        return cls(root=resolved_root, runtime_config=loaded.runtime)

    def submit(self, prompt: str) -> SubmitResult:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")

        request_path = self._write_request(prompt)
        message_path = self.inbox.post(
            "orchestrator",
            {
                "action": "submit_request",
                "request_path": str(request_path.relative_to(self.root)),
                "role": "orchestrator",
            },
        )
        return SubmitResult(request_path=request_path, message_path=message_path)

    def startup_reconcile(self) -> ReconcileResult:
        cleared = self._clear_stale_pid()
        active_tasks = 0
        for path in self.task_store.list_tasks("active"):
            self.task_store.read_path(path)
            active_tasks += 1
        messages = self.inbox.list_messages("orchestrator")
        worktrees = self._count_worktrees()
        return ReconcileResult(
            active_tasks=active_tasks,
            orchestrator_messages=len(messages),
            worktrees=worktrees,
            cleared_stale_pid=cleared,
        )

    def run_once(self) -> RunOnceResult:
        self.startup_reconcile()
        message = self.inbox.read_next("orchestrator")
        if message is not None:
            return self._handle_orchestrator_message(message)

        dispatch = self.dispatcher.dispatch_next()
        if dispatch is None:
            return RunOnceResult(kind="idle", message="no actionable work")
        return RunOnceResult(
            kind="dispatched",
            message=f"dispatched {dispatch.task_id}",
            dispatch=dispatch,
        )

    def _handle_orchestrator_message(self, message: InboxMessage) -> RunOnceResult:
        action = message.body.get("action")
        if action == "submit_request":
            self.inbox.ack(message)
            dispatch = self.dispatcher.dispatch_next()
            if dispatch is None:
                return RunOnceResult(
                    kind="request_recorded",
                    message="request recorded; no ready task",
                    inbox_message=message,
                )
            return RunOnceResult(
                kind="dispatched",
                message=f"dispatched {dispatch.task_id}",
                dispatch=dispatch,
                inbox_message=message,
            )
        if action == "reject_plan":
            self.inbox.ack(message)
            return RunOnceResult(
                kind="plan_rejected",
                message="worker rejected plan; re-planning required",
                inbox_message=message,
            )

        self.inbox.ack(message)
        return RunOnceResult(
            kind="ignored_message",
            message=f"ignored unsupported action: {action!r}",
            inbox_message=message,
        )

    def _write_request(self, prompt: str) -> Path:
        requests_root = self.root / ".orch" / "requests"
        requests_root.mkdir(parents=True, exist_ok=True)
        request_path = requests_root / f"{self._request_id()}.md"
        temp_path = request_path.with_suffix(".md.tmp")
        content = (
            f"# Orchestra Request\n\n"
            f"Submitted: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}\n\n"
            f"{prompt.rstrip()}\n"
        )
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, request_path)
        self._fsync_dir(requests_root)
        return request_path

    def _request_id(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"R-{timestamp}-{uuid4().hex}"

    def _clear_stale_pid(self) -> bool:
        pid_path = self.root / ".orch" / "locks" / "orchestrator.pid"
        if not pid_path.exists():
            return False
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        try:
            pid = int(raw_pid)
        except ValueError:
            pid_path.unlink()
            return True
        if _pid_is_running(pid):
            return False
        pid_path.unlink()
        return True

    def _count_worktrees(self) -> int:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return 0
        return sum(1 for line in result.stdout.splitlines() if line.startswith("worktree "))

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def result_to_dict(result: RunOnceResult) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": result.kind, "message": result.message}
    if result.dispatch is not None:
        data["task_id"] = result.dispatch.task_id
        data["task_path"] = str(result.dispatch.task_path)
        data["worktree_path"] = str(result.dispatch.worktree_path)
        data["message_path"] = str(result.dispatch.message_path)
    if result.inbox_message is not None:
        data["inbox_message"] = str(result.inbox_message.path)
    return data
