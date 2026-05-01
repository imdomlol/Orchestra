"""Runtime loop primitives for the local orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

import yaml

from orch.config import (
    BudgetConfig,
    CriticConfig,
    OrchestraConfig,
    RuntimeConfig,
    load_config,
)
from orch.dispatcher import DispatchResult, Dispatcher
from orch.inbox import Inbox, InboxMessage
from orch.merge import MergeDriver, MergeResult
from orch.model_wrapper import ModelWrapper, WrapperResult
from orch.plans import PlanBudgetExceeded, PlanIngestResult, PlanIngestor
from orch.review import (
    CriticDispatchResult,
    DiffExportResult,
    ReviewDispatcher,
    resolve_critic_mode,
)
from orch.task_store import ReviewNote, TaskStore


class RoleRunner(Protocol):
    def run_role(self, role: str, **context: Any) -> WrapperResult: ...


def ingest_task_yaml(
    yaml_text: str,
    *,
    root: Path = Path("."),
    task_store: TaskStore | None = None,
) -> Path:
    try:
        task = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed task YAML: {exc}") from exc
    if not isinstance(task, dict):
        raise ValueError("task YAML must contain a mapping")

    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task YAML must include a string id")

    store = task_store or TaskStore(root.resolve())
    try:
        store.path_for(task_id)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"task already exists: {task_id}")
    return store.write_pending(task)


@dataclass(frozen=True)
class SubmitResult:
    request_path: Path
    message_path: Path


class PlanOnlyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        result: WrapperResult,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.stderr_path = result.process.stderr_path
        self.returncode = result.process.returncode


class _PlanOnlyHandoffStore:
    def __init__(self, root: Path, log_name: str) -> None:
        self.root = root
        self.log_name = log_name

    def post(self, role: str, body: dict[str, Any]) -> Path:
        log_dir = self.root / ".orch" / "logs" / "planner"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{self.log_name}.handoff.json"
        temp_path = path.with_suffix(".handoff.json.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, indent=2, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            fd = os.open(log_dir, os.O_RDONLY)
        except OSError:
            return path
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return path


class _SyncWorkerHandoffStore:
    def __init__(self, root: Path, task_id: str) -> None:
        self.root = root
        self.task_id = task_id

    def post(self, role: str, body: dict[str, Any]) -> Path:
        log_dir = self.root / ".orch" / "logs" / "workers" / self.task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "sync-handoff.json"
        temp_path = path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, indent=2, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            fd = os.open(log_dir, os.O_RDONLY)
        except OSError:
            return path
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return path


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
    critic_dispatch: CriticDispatchResult | None = None
    planner_result: WrapperResult | None = None
    agent_result: WrapperResult | None = None
    inbox_message: InboxMessage | None = None
    plan_ingest: PlanIngestResult | None = None
    merge_result: MergeResult | None = None


@dataclass(frozen=True)
class RunLoopResult:
    kind: str
    message: str
    iterations: int
    last_result: RunOnceResult | None = None


@dataclass(frozen=True)
class SyncDispatchResult:
    task_id: str
    task_path: Path
    worktree_path: Path
    worker_result: WrapperResult


@dataclass(frozen=True)
class ManualMergeResult:
    task_id: str
    merge_result: MergeResult

    @property
    def merged(self) -> bool:
        return self.merge_result.merged


class OrchestraRuntime:
    """Small deterministic runtime loop over inbox and dispatch state."""

    def __init__(
        self,
        *,
        root: Path = Path("."),
        runtime_config: RuntimeConfig,
        budget_config: BudgetConfig | None = None,
        critic_config: CriticConfig | None = None,
        task_store: TaskStore | None = None,
        inbox: Inbox | None = None,
        dispatcher: Dispatcher | None = None,
        plan_ingestor: PlanIngestor | None = None,
        review_dispatcher: ReviewDispatcher | None = None,
        merge_driver: MergeDriver | None = None,
        model_wrapper: RoleRunner | None = None,
        on_progress: Callable[[str], None] | None = None,
        on_confirm: Callable[[str], bool] | None = None,
        model_stderr_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.runtime_config = runtime_config
        self.budget_config = budget_config or BudgetConfig(
            max_tasks_per_request=5,
            max_wall_clock_minutes=60,
        )
        self.critic_config = critic_config or CriticConfig()
        self.task_store = task_store or TaskStore(self.root)
        self.inbox = inbox or Inbox(self.root)
        self.dispatcher = dispatcher or Dispatcher(
            root=self.root,
            runtime=runtime_config,
            task_store=self.task_store,
            inbox=self.inbox,
        )
        self.plan_ingestor = plan_ingestor or PlanIngestor(
            self.root,
            task_store=self.task_store,
        )
        self.review_dispatcher = review_dispatcher or ReviewDispatcher(
            self.root,
            task_store=self.task_store,
            inbox=self.inbox,
        )
        self.merge_driver = merge_driver or MergeDriver(
            root=self.root,
            task_store=self.task_store,
        )
        self.model_wrapper = model_wrapper
        self._on_progress = on_progress
        self._on_confirm = on_confirm
        self._model_stderr_sink = model_stderr_sink

    def _progress(self, message: str) -> None:
        if self._on_progress is not None:
            self._on_progress(message)

    def _confirm(self, message: str) -> bool:
        if self._on_confirm is not None:
            return self._on_confirm(message)
        return False

    @classmethod
    def from_config(
        cls,
        *,
        root: Path = Path("."),
        config: OrchestraConfig | None = None,
        on_progress: Callable[[str], None] | None = None,
        on_confirm: Callable[[str], bool] | None = None,
        model_stderr_sink: Callable[[str], None] | None = None,
    ) -> "OrchestraRuntime":
        resolved_root = root.resolve()
        loaded = config or load_config(resolved_root / ".orch" / "config")
        merge_driver = MergeDriver.from_config(root=resolved_root, config=loaded)
        return cls(
            root=resolved_root,
            runtime_config=loaded.runtime,
            budget_config=loaded.budgets,
            critic_config=loaded.critic,
            merge_driver=merge_driver,
            on_progress=on_progress,
            on_confirm=on_confirm,
            model_stderr_sink=model_stderr_sink,
        )

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

    def plan_only(self, request: str) -> Path:
        if not request.strip():
            raise ValueError("request must not be empty")

        request_path = self._write_request(request)
        relative_request = str(request_path.relative_to(self.root))
        log_name = request_path.stem
        wrapper = self.model_wrapper or ModelWrapper(
            root=self.root,
            inbox=_PlanOnlyHandoffStore(self.root, log_name),
            stderr_sink=self._model_stderr_sink,
        )
        result = wrapper.run_role(
            "gemini-planner",
            request_path=relative_request,
            log_name=log_name,
            inbox_role="plan-only",
        )
        self._discard_handoff_message(result.handoff_path)
        if not result.process.succeeded:
            raise PlanOnlyError("planner exited unsuccessfully", result=result)
        if result.handoff is None:
            raise PlanOnlyError(
                "planner did not emit an Orchestra handoff",
                result=result,
            )

        plan_path = result.handoff.get("plan_path")
        if not isinstance(plan_path, str) or not plan_path.strip():
            raise PlanOnlyError(
                "planner handoff did not include plan_path",
                result=result,
            )
        self._materialize_planned_content(
            InboxMessage(
                role="plan-only",
                path=result.handoff_path or Path(),
                body=result.handoff,
            ),
            plan_path,
        )
        resolved_plan = self._resolve_plan_path(plan_path)
        if not resolved_plan.exists():
            raise PlanOnlyError(
                "planner handoff referenced a missing plan file",
                result=result,
            )
        return resolved_plan

    def dispatch_task(self, task_id: str) -> SyncDispatchResult:
        pending_path = self.task_store.tasks_root / "pending" / f"{task_id}.yaml"
        with self.task_store.pickup_lock(task_id):
            if not pending_path.exists():
                raise FileNotFoundError(f"pending task not found: {task_id}")

            task = self.task_store.read_path(pending_path)
            if not self.dispatcher._dependencies_merged(task):
                raise RuntimeError(f"{task_id} has unmerged dependencies")
            if self.dispatcher._collides_with_active(
                task, self.dispatcher._active_tasks()
            ):
                raise RuntimeError(f"{task_id} owned_files collide with an active task")

            info = self.dispatcher.worktrees.create(
                task_id,
                self.dispatcher.base_ref,
                owned_files=task.get("owned_files", []),
                forbidden_files=task.get("forbidden_files", []),
            )
            active_path = self.task_store.transition(task_id, "active", "in_progress")

        result = self._run_worker_sync(task_id, active_path, info.path)
        return SyncDispatchResult(
            task_id=task_id,
            task_path=active_path,
            worktree_path=info.path,
            worker_result=result,
        )

    def export_diff(self, task_id: str) -> DiffExportResult:
        return self.review_dispatcher.export_diff(task_id)

    def rework_task(self, task_id: str, notes: str) -> SyncDispatchResult:
        if not notes.strip():
            raise ValueError("notes must not be empty")
        self.task_store.append_review_note(
            task_id,
            ReviewNote(
                author="claude-orchestrator",
                verdict="request_changes",
                body=notes,
            ),
        )
        active_path = self.task_store.transition(task_id, "active", "in_progress")
        task = self.task_store.read(task_id)
        worktree_path = (self.root / task["worktree_path"]).resolve()
        result = self._run_worker_sync(task_id, active_path, worktree_path)
        return SyncDispatchResult(
            task_id=task_id,
            task_path=active_path,
            worktree_path=worktree_path,
            worker_result=result,
        )

    def merge_task(self, task_id: str) -> ManualMergeResult:
        self.task_store.transition(task_id, "active", "integration_review")
        self._progress(f"Merging {task_id}...")
        try:
            merge = self.merge_driver.merge_task(task_id)
        except Exception as exc:
            self.task_store.append_review_note(
                task_id,
                ReviewNote(
                    author="claude-orchestrator",
                    verdict="request_changes",
                    body=f"Integration failed: {exc}",
                ),
            )
            raise
        if not merge.merged:
            self.task_store.append_review_note(
                task_id,
                ReviewNote(
                    author="claude-orchestrator",
                    verdict="request_changes",
                    body=f"Integration failed: {merge.status}: {merge.message}",
                ),
            )
        return ManualMergeResult(task_id=task_id, merge_result=merge)

    def _run_worker_sync(
        self,
        task_id: str,
        task_path: Path,
        worktree_path: Path,
    ) -> WrapperResult:
        task_yaml_path = str(task_path.relative_to(self.root))
        relative_worktree = str(worktree_path.relative_to(self.root))
        wrapper = self.model_wrapper or ModelWrapper(
            root=self.root,
            inbox=_SyncWorkerHandoffStore(self.root, task_id),
            stderr_sink=self._model_stderr_sink,
        )
        self._progress(f"Calling codex-worker on {task_id}...")
        result = wrapper.run_role(
            "codex-worker",
            log_name=task_id,
            inbox_role="orchestrator",
            task_id=task_id,
            task_yaml_path=task_yaml_path,
            worktree_path=relative_worktree,
        )
        self._progress(f"codex-worker returned (exit {result.process.returncode})")
        if not result.succeeded:
            reason = (
                f"codex-worker exited with {result.process.returncode}"
                if not result.process.succeeded
                else "codex-worker did not emit an Orchestra handoff"
            )
            self.task_store.append_review_note(
                task_id,
                ReviewNote(
                    author="claude-orchestrator",
                    verdict="request_changes",
                    body=reason,
                ),
            )
            raise RuntimeError(f"{task_id} dispatch failed: {reason}")

        task_after_worker = self.task_store.read(task_id)
        if task_after_worker.get("status") == "in_progress":
            self.task_store.transition(task_id, "active", "self_review")
        return result

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

        agent_message = self._next_agent_message()
        if agent_message is not None:
            return self._drive_agent_message(agent_message)

        dispatch = self.dispatcher.dispatch_next()
        if dispatch is None:
            return RunOnceResult(kind="idle", message="no actionable work")
        return RunOnceResult(
            kind="dispatched",
            message=f"dispatched {dispatch.task_id}",
            dispatch=dispatch,
        )

    def run(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        on_result: Callable[[RunOnceResult], None] | None = None,
        max_idle_cycles: int | None = None,
    ) -> RunLoopResult:
        """Continuously process runtime events until stopped.

        ``max_idle_cycles`` is primarily for tests and controlled embedding;
        ``None`` means wait indefinitely for future work.
        """

        if max_idle_cycles is not None and max_idle_cycles < 1:
            raise ValueError("max_idle_cycles must be >= 1")

        stop = stop_requested or (lambda: False)
        iterations = 0
        idle_cycles = 0
        last_result: RunOnceResult | None = None
        deadline = monotonic() + (self.budget_config.max_wall_clock_minutes * 60)
        self._write_pid()
        self._append_orchestrator_log("run_started", {"pid": os.getpid()})
        try:
            while not stop():
                if monotonic() >= deadline and self._has_recoverable_work():
                    result = RunOnceResult(
                        kind="budget_exceeded",
                        message=(
                            "max_wall_clock_minutes exceeded; recoverable work remains"
                        ),
                    )
                    iterations += 1
                    last_result = result
                    self._append_orchestrator_log(
                        "budget_exceeded",
                        {
                            "budget": "max_wall_clock_minutes",
                            "limit": self.budget_config.max_wall_clock_minutes,
                        },
                    )
                    if on_result is not None:
                        on_result(result)
                    return RunLoopResult(
                        kind="budget_exceeded",
                        message=result.message,
                        iterations=iterations,
                        last_result=last_result,
                    )

                result = self.run_once()
                iterations += 1
                last_result = result
                if on_result is not None:
                    on_result(result)

                if result.kind == "budget_exceeded":
                    return RunLoopResult(
                        kind="budget_exceeded",
                        message=result.message,
                        iterations=iterations,
                        last_result=last_result,
                    )

                if result.kind == "idle":
                    idle_cycles += 1
                    if max_idle_cycles is not None and idle_cycles >= max_idle_cycles:
                        return RunLoopResult(
                            kind="idle",
                            message="run loop idle",
                            iterations=iterations,
                            last_result=last_result,
                        )
                    sleep(self.runtime_config.poll_interval_seconds)
                else:
                    idle_cycles = 0

            return RunLoopResult(
                kind="stopped",
                message="run loop stopped",
                iterations=iterations,
                last_result=last_result,
            )
        finally:
            self._append_orchestrator_log(
                "run_shutdown",
                {"pid": os.getpid(), "iterations": iterations},
            )
            self._remove_pid()

    def _handle_orchestrator_message(self, message: InboxMessage) -> RunOnceResult:
        action = message.body.get("action")
        if action == "submit_request":
            return self._handle_submit_request(message)
        if action == "reject_plan":
            self.inbox.ack(message)
            return RunOnceResult(
                kind="plan_rejected",
                message="worker rejected plan; re-planning required",
                inbox_message=message,
            )
        if action == "planned":
            return self._handle_planned(message)
        if action == "worker_completed":
            task_id = message.body.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("worker_completed message must include task_id")
            return self._handle_worker_completed(message, task_id)
        if action == "critic_reviewed":
            return self._handle_critic_reviewed(message)

        self.inbox.ack(message)
        return RunOnceResult(
            kind="ignored_message",
            message=f"ignored unsupported action: {action!r}",
            inbox_message=message,
        )

    def _handle_worker_completed(
        self,
        message: InboxMessage,
        task_id: str,
    ) -> RunOnceResult:
        task = self.task_store.read(task_id)
        mode = resolve_critic_mode(task, self.critic_config.mode)

        if mode == "opus":
            self.task_store.transition(task_id, "active", "self_review")
            self.inbox.ack(message)
            return RunOnceResult(
                kind="self_review",
                message=f"{task_id} awaiting Opus review",
                inbox_message=message,
            )

        if mode == "both":
            critic = self.review_dispatcher.dispatch_to_critic_for_opus(task_id)
            self.inbox.ack(message)
            return RunOnceResult(
                kind="critic_dispatched",
                message=f"sent {task_id} to Gemini critic; awaiting Opus review",
                critic_dispatch=critic,
                inbox_message=message,
            )

        critic = self.review_dispatcher.dispatch_to_critic(task_id)
        self.inbox.ack(message)
        return RunOnceResult(
            kind="critic_dispatched",
            message=f"sent {task_id} to critic review",
            critic_dispatch=critic,
            inbox_message=message,
        )

    def _handle_submit_request(self, message: InboxMessage) -> RunOnceResult:
        request_path = message.body.get("request_path")
        if not isinstance(request_path, str) or not request_path.strip():
            raise ValueError("submit_request message must include request_path")

        existing_plan = self._find_planned_for_request(request_path)
        if existing_plan is not None:
            try:
                ingest = self._ingest_planned_message(existing_plan)
            except PlanBudgetExceeded as exc:
                self.inbox.ack(message)
                self._record_budget_exceeded(
                    "max_tasks_per_request",
                    {
                        "limit": exc.max_tasks,
                        "actual": exc.task_count,
                        "plan_message": str(existing_plan.path.relative_to(self.root)),
                    },
                )
                return RunOnceResult(
                    kind="budget_exceeded",
                    message=str(exc),
                    inbox_message=existing_plan,
                )
            self.inbox.ack(message)
            self.inbox.ack(existing_plan)
            return RunOnceResult(
                kind="plan_ingested",
                message=f"planned and ingested {ingest.task_count} tasks",
                inbox_message=message,
                plan_ingest=ingest,
            )

        planner_wrapper = self.model_wrapper or ModelWrapper(
            root=self.root,
            inbox=self.inbox,
            stderr_sink=self._model_stderr_sink,
        )
        self._progress("Calling Gemini planner...")
        planner = planner_wrapper.run_role(
            "gemini-planner",
            request_path=request_path,
            log_name=Path(request_path).stem,
            inbox_role="orchestrator",
        )
        self._progress(f"Gemini planner returned (exit {planner.process.returncode})")
        if not planner.process.succeeded or planner.handoff is None or planner.handoff_path is None:
            failure_reason = (
                "planner exited unsuccessfully"
                if not planner.process.succeeded
                else "planner did not emit an Orchestra handoff"
            )
            if self._confirm(f"Gemini planner failed ({failure_reason}). Use Claude Opus as fallback planner?"):
                self._progress("Calling Claude Opus planner...")
                planner = planner_wrapper.run_role(
                    "claude-planner",
                    request_path=request_path,
                    log_name=Path(request_path).stem,
                    inbox_role="orchestrator",
                )
                self._progress(f"Claude planner returned (exit {planner.process.returncode})")
            if not planner.process.succeeded:
                self.inbox.ack(message)
                return RunOnceResult(
                    kind="planning_failed",
                    message="planner exited unsuccessfully",
                    planner_result=planner,
                    inbox_message=message,
                )
            if planner.handoff is None or planner.handoff_path is None:
                self.inbox.ack(message)
                return RunOnceResult(
                    kind="planning_failed",
                    message="planner did not emit an Orchestra handoff",
                    planner_result=planner,
                    inbox_message=message,
                )

        planned_message = self.inbox.read_path(planner.handoff_path)
        if planned_message.body.get("action") != "planned":
            self.inbox.ack(message)
            self.inbox.ack(planned_message)
            return RunOnceResult(
                kind="planning_failed",
                message="planner handoff was not a planned action",
                planner_result=planner,
                inbox_message=message,
            )

        try:
            ingest = self._ingest_planned_message(planned_message)
        except PlanBudgetExceeded as exc:
            self.inbox.ack(message)
            self._record_budget_exceeded(
                "max_tasks_per_request",
                {
                    "limit": exc.max_tasks,
                    "actual": exc.task_count,
                    "plan_message": str(planned_message.path.relative_to(self.root)),
                },
            )
            return RunOnceResult(
                kind="budget_exceeded",
                message=str(exc),
                planner_result=planner,
                inbox_message=planned_message,
            )
        self.inbox.ack(message)
        self.inbox.ack(planned_message)
        return RunOnceResult(
            kind="plan_ingested",
            message=f"planned and ingested {ingest.task_count} tasks",
            planner_result=planner,
            inbox_message=message,
            plan_ingest=ingest,
        )

    def _find_planned_for_request(self, request_path: str) -> InboxMessage | None:
        for candidate in self.inbox.list_messages("orchestrator"):
            if candidate.body.get("action") != "planned":
                continue
            candidate_request = candidate.body.get("request_path")
            if candidate_request == request_path:
                return candidate
        return None

    def _handle_planned(self, message: InboxMessage) -> RunOnceResult:
        try:
            ingest = self._ingest_planned_message(message)
        except PlanBudgetExceeded as exc:
            self._record_budget_exceeded(
                "max_tasks_per_request",
                {"limit": exc.max_tasks, "actual": exc.task_count},
            )
            return RunOnceResult(
                kind="budget_exceeded",
                message=str(exc),
                inbox_message=message,
            )
        self.inbox.ack(message)
        dispatch = self.dispatcher.dispatch_next()
        if dispatch is not None:
            return RunOnceResult(
                kind="dispatched",
                message=f"ingested {ingest.task_count} tasks; dispatched {dispatch.task_id}",
                dispatch=dispatch,
                inbox_message=message,
                plan_ingest=ingest,
            )
        return RunOnceResult(
            kind="plan_ingested",
            message=f"ingested {ingest.task_count} tasks",
            inbox_message=message,
            plan_ingest=ingest,
        )

    def _next_agent_message(self) -> InboxMessage | None:
        for role in ("worker", "critic", "integrator"):
            message = self.inbox.read_next(role)
            if message is not None:
                return message
        return None

    def _drive_agent_message(self, message: InboxMessage) -> RunOnceResult:
        wrapper_role = {
            "worker": "codex-worker",
            "critic": "gemini-critic",
            "integrator": "codex-integrator",
        }.get(message.role)
        if wrapper_role is None:
            raise ValueError(f"unsupported agent inbox role: {message.role}")

        wrapper = self.model_wrapper or ModelWrapper(root=self.root, inbox=self.inbox, stderr_sink=self._model_stderr_sink)
        context = dict(message.body)
        context.pop("role", None)
        task_id = context.get("task_id")
        log_name = task_id if isinstance(task_id, str) and task_id.strip() else message.id
        label = f"{wrapper_role} on {task_id}" if task_id else wrapper_role
        self._progress(f"Calling {label}...")
        result = wrapper.run_role(
            wrapper_role,
            log_name=log_name,
            inbox_role="orchestrator",
            **context,
        )
        self._progress(f"{wrapper_role} returned (exit {result.process.returncode})")
        if not result.succeeded:
            return RunOnceResult(
                kind="agent_failed",
                message=f"{wrapper_role} failed; role inbox message left for retry",
                agent_result=result,
                inbox_message=message,
            )

        self.inbox.ack(message)
        return RunOnceResult(
            kind="agent_ran",
            message=f"{wrapper_role} completed",
            agent_result=result,
            inbox_message=message,
        )

    def _ingest_planned_message(self, message: InboxMessage) -> PlanIngestResult:
        plan_path = message.body.get("plan_path")
        if not isinstance(plan_path, str) or not plan_path.strip():
            raise ValueError("planned message must include plan_path")
        self._materialize_planned_content(message, plan_path)
        return self.plan_ingestor.ingest(
            plan_path,
            max_tasks=self.budget_config.max_tasks_per_request,
        )

    def _materialize_planned_content(self, message: InboxMessage, plan_path: str) -> None:
        plan_content = message.body.get("plan_content")
        resolved_plan = self._resolve_plan_path(plan_path)

        if resolved_plan.exists():
            return
        if isinstance(plan_content, str) and plan_content:
            resolved_plan.parent.mkdir(parents=True, exist_ok=True)
            resolved_plan.write_text(plan_content, encoding="utf-8")
            return
        raise ValueError(
            "planner handoff referenced a missing plan file without plan_content: "
            f"{plan_path}"
        )

    def _resolve_plan_path(self, plan_path: str) -> Path:
        path = Path(plan_path)
        if path.is_absolute():
            resolved_plan = path.resolve()
        else:
            resolved_plan = (self.root / path).resolve()
        plans_root = (self.root / ".orch" / "plans").resolve()
        try:
            resolved_plan.relative_to(plans_root)
        except ValueError as exc:
            raise ValueError(f"plan path is outside .orch/plans: {plan_path}") from exc
        return resolved_plan

    def _discard_handoff_message(self, path: Path | None) -> None:
        if path is None or not path.exists():
            return
        inbox_root = (self.root / ".orch" / "inbox").resolve()
        try:
            path.resolve().relative_to(inbox_root)
        except ValueError:
            return
        path.unlink()
        self._fsync_dir(path.parent)

    def _record_budget_exceeded(self, budget: str, payload: dict[str, Any]) -> Path:
        return self._append_orchestrator_log(
            "budget_exceeded",
            {"budget": budget, **payload},
        )

    def _has_recoverable_work(self) -> bool:
        for role in ("orchestrator", "worker", "critic", "integrator"):
            if self.inbox.list_messages(role):
                return True
        for state in ("pending", "active"):
            if self.task_store.list_tasks(state):
                return True
        return False

    def _handle_critic_reviewed(self, message: InboxMessage) -> RunOnceResult:
        body = message.body
        task_id = body.get("task_id")
        verdict = body.get("verdict")
        note_body = str(body.get("body") or verdict)

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("critic_reviewed message must include task_id")
        if verdict not in {"approve", "request_changes", "reject"}:
            raise ValueError(
                f"critic_reviewed message must have a valid verdict, got {verdict!r}"
            )

        task = self.task_store.read(task_id)
        mode = resolve_critic_mode(task, self.critic_config.mode)
        if mode == "both" or (
            task.get("status") == "self_review" and mode != "gemini"
        ):
            self.task_store.append_review_note(
                task_id,
                ReviewNote(author="gemini-critic", verdict=verdict, body=note_body),
            )
            self.task_store.transition(task_id, "active", "self_review")
            self.inbox.ack(message)
            return RunOnceResult(
                kind="self_review",
                message=f"recorded Gemini critic verdict for {task_id}; awaiting Opus review",
                inbox_message=message,
            )

        # Count prior critic request_changes rounds before appending the new note.
        prior_critic_rounds = sum(
            1
            for note in task.get("review_notes", [])
            if note.get("author") == "gemini-critic"
            and note.get("verdict") == "request_changes"
        )

        self.task_store.append_review_note(
            task_id, ReviewNote(author="gemini-critic", verdict=verdict, body=note_body)
        )

        if verdict == "approve":
            self.task_store.transition(task_id, "active", "integration_review")
            self._progress(f"Critic approved {task_id}, merging...")
            merge = self.merge_driver.merge_task(task_id)
            if merge.merged:
                self.inbox.ack(message)
                return RunOnceResult(
                    kind="merged",
                    message=f"merged {task_id}",
                    merge_result=merge,
                    inbox_message=message,
                )
            # Integration failed: count failures to decide whether to escalate.
            task_after = self.task_store.read(task_id)
            prior_integration_failures = sum(
                1
                for note in task_after.get("review_notes", [])
                if note.get("author") == "codex-integrator"
                and note.get("verdict") == "request_changes"
            )
            if prior_integration_failures >= self.runtime_config.max_retries:
                self.task_store.transition(task_id, "active", "blocked")
                self.inbox.ack(message)
                return RunOnceResult(
                    kind="escalated",
                    message=(
                        f"{task_id} blocked after {prior_integration_failures} "
                        "integration failures"
                    ),
                    merge_result=merge,
                    inbox_message=message,
                )
            self._redispatch_to_worker(task_id)
            self.inbox.ack(message)
            return RunOnceResult(
                kind="merge_failed_reworking",
                message=f"integration failed for {task_id}; routed back to worker",
                merge_result=merge,
                inbox_message=message,
            )

        if verdict == "request_changes":
            if prior_critic_rounds >= self.runtime_config.max_retries:
                self.task_store.transition(task_id, "active", "blocked")
                self.inbox.ack(message)
                return RunOnceResult(
                    kind="escalated",
                    message=(
                        f"{task_id} blocked after {prior_critic_rounds + 1} critic rounds"
                    ),
                    inbox_message=message,
                )
            self._redispatch_to_worker(task_id)
            self.inbox.ack(message)
            return RunOnceResult(
                kind="critic_rework_dispatched",
                message=(
                    f"critic requested changes for {task_id}; "
                    f"routed to worker (round {prior_critic_rounds + 1})"
                ),
                inbox_message=message,
            )

        # verdict == "reject"
        self.task_store.transition(task_id, "done", "abandoned")
        self.inbox.ack(message)
        return RunOnceResult(
            kind="abandoned",
            message=f"{task_id} abandoned after critic rejection",
            inbox_message=message,
        )

    def _redispatch_to_worker(self, task_id: str) -> Path:
        task = self.task_store.read(task_id)
        task_path = self.task_store.transition(task_id, "active", "in_progress")
        return self.inbox.post(
            "worker",
            {
                "task_id": task_id,
                "task_yaml_path": str(task_path.relative_to(self.root)),
                "worktree_path": task["worktree_path"],
                "role": "worker",
            },
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

    def _write_pid(self) -> Path:
        pid_path = self.root / ".orch" / "locks" / "orchestrator.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        if pid_path.exists():
            raw_pid = pid_path.read_text(encoding="utf-8").strip()
            try:
                existing_pid = int(raw_pid)
            except ValueError:
                existing_pid = 0
            if _pid_is_running(existing_pid):
                raise RuntimeError(f"orchestrator already running with pid {existing_pid}")
            pid_path.unlink()

        temp_path = pid_path.with_suffix(".pid.tmp")
        temp_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        os.replace(temp_path, pid_path)
        self._fsync_dir(pid_path.parent)
        return pid_path

    def _remove_pid(self) -> None:
        pid_path = self.root / ".orch" / "locks" / "orchestrator.pid"
        if not pid_path.exists():
            return
        if pid_path.read_text(encoding="utf-8").strip() != str(os.getpid()):
            return
        pid_path.unlink()
        self._fsync_dir(pid_path.parent)

    def _append_orchestrator_log(self, event: str, payload: dict[str, Any]) -> Path:
        log_dir = self.root / ".orch" / "logs" / "orchestrator"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"
        entry = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "event": event,
            **payload,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return log_path

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
        if result.dispatch.message_path is not None:
            data["message_path"] = str(result.dispatch.message_path)
    if result.critic_dispatch is not None:
        data["task_id"] = result.critic_dispatch.task_id
        data["task_path"] = str(result.critic_dispatch.task_path)
        data["diff_path"] = str(result.critic_dispatch.diff_path)
        data["message_path"] = str(result.critic_dispatch.message_path)
    if result.planner_result is not None:
        data["planner_returncode"] = result.planner_result.process.returncode
        data["planner_stdout_path"] = str(result.planner_result.process.stdout_path)
        data["planner_stderr_path"] = str(result.planner_result.process.stderr_path)
        if result.planner_result.handoff_path is not None:
            data["planner_handoff_path"] = str(result.planner_result.handoff_path)
    if result.agent_result is not None:
        data["agent_role"] = result.agent_result.role
        data["agent_returncode"] = result.agent_result.process.returncode
        data["agent_stdout_path"] = str(result.agent_result.process.stdout_path)
        data["agent_stderr_path"] = str(result.agent_result.process.stderr_path)
        if result.agent_result.handoff_path is not None:
            data["agent_handoff_path"] = str(result.agent_result.handoff_path)
    if result.inbox_message is not None:
        data["inbox_message"] = str(result.inbox_message.path)
    if result.plan_ingest is not None:
        data["plan_path"] = str(result.plan_ingest.plan_path)
        data["task_paths"] = [str(path) for path in result.plan_ingest.task_paths]
        data["task_count"] = result.plan_ingest.task_count
    if result.merge_result is not None:
        data["merge_status"] = result.merge_result.status
        data["patch_path"] = str(result.merge_result.patch_path)
    return data
