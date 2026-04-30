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

from orch.config import BudgetConfig, OrchestraConfig, RuntimeConfig, load_config
from orch.dispatcher import DispatchResult, Dispatcher
from orch.inbox import Inbox, InboxMessage
from orch.merge import MergeDriver, MergeResult
from orch.model_wrapper import ModelWrapper, WrapperResult
from orch.plans import PlanBudgetExceeded, PlanIngestResult, PlanIngestor
from orch.review import CriticDispatchResult, ReviewDispatcher
from orch.task_store import ReviewNote, TaskStore


class RoleRunner(Protocol):
    def run_role(self, role: str, **context: Any) -> WrapperResult: ...


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


class OrchestraRuntime:
    """Small deterministic runtime loop over inbox and dispatch state."""

    def __init__(
        self,
        *,
        root: Path = Path("."),
        runtime_config: RuntimeConfig,
        budget_config: BudgetConfig | None = None,
        task_store: TaskStore | None = None,
        inbox: Inbox | None = None,
        dispatcher: Dispatcher | None = None,
        plan_ingestor: PlanIngestor | None = None,
        review_dispatcher: ReviewDispatcher | None = None,
        merge_driver: MergeDriver | None = None,
        model_wrapper: RoleRunner | None = None,
    ) -> None:
        self.root = root.resolve()
        self.runtime_config = runtime_config
        self.budget_config = budget_config or BudgetConfig(
            max_tasks_per_request=5,
            max_wall_clock_minutes=60,
        )
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

    @classmethod
    def from_config(
        cls,
        *,
        root: Path = Path("."),
        config: OrchestraConfig | None = None,
    ) -> "OrchestraRuntime":
        resolved_root = root.resolve()
        loaded = config or load_config(resolved_root / ".orch" / "config")
        merge_driver = MergeDriver.from_config(root=resolved_root, config=loaded)
        return cls(
            root=resolved_root,
            runtime_config=loaded.runtime,
            budget_config=loaded.budgets,
            merge_driver=merge_driver,
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
            critic = self.review_dispatcher.dispatch_to_critic(task_id)
            self.inbox.ack(message)
            return RunOnceResult(
                kind="critic_dispatched",
                message=f"sent {task_id} to critic review",
                critic_dispatch=critic,
                inbox_message=message,
            )
        if action == "critic_reviewed":
            return self._handle_critic_reviewed(message)

        self.inbox.ack(message)
        return RunOnceResult(
            kind="ignored_message",
            message=f"ignored unsupported action: {action!r}",
            inbox_message=message,
        )

    def _handle_submit_request(self, message: InboxMessage) -> RunOnceResult:
        request_path = message.body.get("request_path")
        if not isinstance(request_path, str) or not request_path.strip():
            raise ValueError("submit_request message must include request_path")

        planner_wrapper = self.model_wrapper or ModelWrapper(
            root=self.root,
            inbox=self.inbox,
        )
        planner = planner_wrapper.run_role(
            "gemini-planner",
            request_path=request_path,
            log_name=Path(request_path).stem,
            inbox_role="orchestrator",
        )
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

        wrapper = self.model_wrapper or ModelWrapper(root=self.root, inbox=self.inbox)
        context = dict(message.body)
        context.pop("role", None)
        task_id = context.get("task_id")
        log_name = task_id if isinstance(task_id, str) and task_id.strip() else message.id
        result = wrapper.run_role(
            wrapper_role,
            log_name=log_name,
            inbox_role="orchestrator",
            **context,
        )
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
        return self.plan_ingestor.ingest(
            plan_path,
            max_tasks=self.budget_config.max_tasks_per_request,
        )

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

        # Count prior critic request_changes rounds before appending the new note.
        task = self.task_store.read(task_id)
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
