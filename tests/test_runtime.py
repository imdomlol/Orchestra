from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import yaml

from orch.config import BudgetConfig, RuntimeConfig
from orch.inbox import Inbox
from orch.model_wrapper import WrapperResult
from orch.runner import ProcessResult
from orch.runtime import OrchestraRuntime
from orch.task_store import TaskStore


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.local")
    git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def copy_runtime_layout(repo: Path) -> None:
    for directory in [
        ".orch/tasks/pending",
        ".orch/tasks/active",
        ".orch/tasks/done",
        ".orch/locks",
        ".orch/requests",
        ".orch/plans",
        ".orch/schemas",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy(".orch/schemas/task.schema.json", repo / ".orch/schemas/task.schema.json")


def load_task(task_id: str = "T-0001") -> dict:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text(encoding="utf-8"))
    task["id"] = task_id
    task["branch"] = f"task/{task_id}"
    task["worktree_path"] = f".orch/worktrees/{task_id}"
    return task


def runtime(repo: Path) -> OrchestraRuntime:
    return OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
            poll_interval_seconds=1,
        ),
    )


def low_task_budget_runtime(repo: Path) -> OrchestraRuntime:
    return OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
            poll_interval_seconds=1,
        ),
        budget_config=BudgetConfig(max_tasks_per_request=1, max_wall_clock_minutes=60),
    )


def process_result(
    repo: Path,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    log_role: str = "planner",
    argv: tuple[str, ...] = ("fake-planner",),
) -> ProcessResult:
    log_dir = repo / ".orch/logs" / log_role
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{log_role}.stdout"
    stderr_path = log_dir / f"{log_role}.stderr"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return ProcessResult(
        argv=argv,
        cwd=repo,
        returncode=returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timed_out=False,
        duration_seconds=0.01,
    )


class FakePlannerWrapper:
    def __init__(self, result: WrapperResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def run_role(self, role: str, **context) -> WrapperResult:
        self.calls.append({"role": role, **context})
        return self.result


class SequentialWrapper:
    def __init__(self, repo: Path, results: list[WrapperResult]) -> None:
        self.repo = repo
        self.results = results
        self.calls: list[dict] = []

    def run_role(self, role: str, **context) -> WrapperResult:
        self.calls.append({"role": role, **context})
        if not self.results:
            raise AssertionError("unexpected wrapper call")
        result = self.results.pop(0)
        if result.handoff is not None and result.handoff_path is None:
            result = replace(
                result,
                handoff_path=Inbox(self.repo).post("orchestrator", result.handoff),
            )
        return result


def planner_result(
    repo: Path,
    *,
    handoff: dict | None,
    returncode: int = 0,
    stderr: str = "",
) -> WrapperResult:
    handoff_path = None
    if handoff is not None:
        handoff_path = Inbox(repo).post("orchestrator", handoff)
    return WrapperResult(
        role="gemini-planner",
        process=process_result(repo, returncode=returncode, stderr=stderr),
        handoff_path=handoff_path,
        handoff=handoff,
    )


def agent_result(
    repo: Path,
    *,
    role: str,
    handoff: dict | None,
    returncode: int = 0,
    stderr: str = "",
) -> WrapperResult:
    return WrapperResult(
        role=role,
        process=process_result(
            repo,
            returncode=returncode,
            stderr=stderr,
            log_role=role,
            argv=(f"fake-{role}",),
        ),
        handoff_path=None,
        handoff=handoff,
    )


def test_submit_writes_request_and_posts_orchestrator_nudge(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)

    result = runtime(repo).submit("Please add a feature.")

    assert result.request_path.exists()
    assert "Please add a feature." in result.request_path.read_text(encoding="utf-8")
    message = Inbox(repo).read_next("orchestrator")
    assert message is not None
    assert message.body == {
        "action": "submit_request",
        "request_path": str(result.request_path.relative_to(repo)),
        "role": "orchestrator",
    }


def test_run_once_processes_oldest_inbox_before_dispatch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())
    inbox = Inbox(repo)
    first = inbox.post("orchestrator", {"action": "unknown"})
    inbox.post("orchestrator", {"action": "submit_request", "request_path": ".orch/requests/R.md"})

    result = runtime(repo).run_once()

    assert result.kind == "ignored_message"
    assert not first.exists()
    assert store.read("T-0001")["status"] == "pending"
    assert len(inbox.list_messages("orchestrator")) == 1


def test_run_once_invokes_planner_and_ingests_plan(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    plan_path = repo / ".orch/plans/P-0001.md"
    plan_path.write_text(
        f"# Plan\n\n```yaml\n{yaml.safe_dump(task, sort_keys=False)}```",
        encoding="utf-8",
    )
    inbox = Inbox(repo)
    inbox.post(
        "orchestrator",
        {"action": "submit_request", "request_path": ".orch/requests/R.md"},
    )
    wrapper = FakePlannerWrapper(
        planner_result(
            repo,
            handoff={"action": "planned", "plan_path": ".orch/plans/P-0001.md"},
        )
    )

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "plan_ingested"
    assert result.plan_ingest is not None
    assert result.plan_ingest.task_count == 1
    assert result.dispatch is None
    assert TaskStore(repo).read("T-0001")["status"] == "pending"
    assert inbox.read_next("worker") is None
    assert inbox.read_next("orchestrator") is None
    assert wrapper.calls == [
        {
            "role": "gemini-planner",
            "request_path": ".orch/requests/R.md",
            "log_name": "R",
            "inbox_role": "orchestrator",
        }
    ]


def test_run_once_planner_missing_handoff_acks_request(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post(
        "orchestrator",
        {"action": "submit_request", "request_path": ".orch/requests/R.md"},
    )
    wrapper = FakePlannerWrapper(planner_result(repo, handoff=None))

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "planning_failed"
    assert "handoff" in result.message
    assert inbox.read_next("orchestrator") is None


def test_run_once_planner_nonzero_acks_request_and_preserves_logs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post(
        "orchestrator",
        {"action": "submit_request", "request_path": ".orch/requests/R.md"},
    )
    wrapper = FakePlannerWrapper(
        planner_result(repo, handoff=None, returncode=2, stderr="planner exploded\n")
    )

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "planning_failed"
    assert result.planner_result is not None
    assert (
        result.planner_result.process.stderr_path.read_text(encoding="utf-8")
        == "planner exploded\n"
    )
    assert inbox.read_next("orchestrator") is None


def test_run_once_dispatches_ready_task_when_no_inbox_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())

    result = runtime(repo).run_once()

    assert result.kind == "dispatched"
    assert store.read("T-0001")["status"] == "in_progress"


def test_run_once_drives_worker_inbox_with_wrapper(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    worker_message = inbox.post(
        "worker",
        {
            "task_id": "T-0001",
            "task_yaml_path": ".orch/tasks/active/T-0001.yaml",
            "worktree_path": ".orch/worktrees/T-0001",
            "role": "worker",
        },
    )
    wrapper = SequentialWrapper(
        repo,
        [
            agent_result(
                repo,
                role="codex-worker",
                handoff={"action": "worker_completed", "task_id": "T-0001"},
            )
        ]
    )

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "agent_ran"
    assert not worker_message.exists()
    assert inbox.read_next("orchestrator") is not None
    assert wrapper.calls == [
        {
            "role": "codex-worker",
            "log_name": "T-0001",
            "inbox_role": "orchestrator",
            "task_id": "T-0001",
            "task_yaml_path": ".orch/tasks/active/T-0001.yaml",
            "worktree_path": ".orch/worktrees/T-0001",
        }
    ]


def test_run_once_failed_agent_wrapper_leaves_role_message_for_retry(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post(
        "critic",
        {
            "task_id": "T-0001",
            "task_yaml_path": ".orch/tasks/active/T-0001.yaml",
            "diff_path": ".orch/patches/T-0001.diff",
            "role": "critic",
        },
    )
    wrapper = SequentialWrapper(
        repo,
        [agent_result(repo, role="gemini-critic", handoff=None, returncode=2)]
    )

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "agent_failed"
    assert inbox.read_next("critic") is not None
    assert inbox.read_next("orchestrator") is None


def test_run_once_drives_agent_inbox_before_dispatch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())
    inbox = Inbox(repo)
    inbox.post(
        "worker",
        {
            "task_id": "T-9999",
            "task_yaml_path": ".orch/tasks/active/T-9999.yaml",
            "worktree_path": ".orch/worktrees/T-9999",
            "role": "worker",
        },
    )
    wrapper = SequentialWrapper(
        repo,
        [
            agent_result(
                repo,
                role="codex-worker",
                handoff={"action": "worker_completed", "task_id": "T-9999"},
            )
        ]
    )

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "agent_ran"
    assert store.read("T-0001")["status"] == "pending"


def test_run_once_ingests_planner_handoff_then_dispatches(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    plan_path = repo / ".orch/plans/P-0001.md"
    plan_path.write_text(
        f"# Plan\n\n```yaml\n{yaml.safe_dump(task, sort_keys=False)}```",
        encoding="utf-8",
    )
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "planned", "plan_path": ".orch/plans/P-0001.md"})

    result = runtime(repo).run_once()

    assert result.kind == "dispatched"
    assert result.plan_ingest is not None
    assert result.plan_ingest.task_count == 1
    assert result.dispatch is not None
    assert result.dispatch.task_id == "T-0001"
    assert TaskStore(repo).read("T-0001")["status"] == "in_progress"
    assert inbox.read_next("orchestrator") is None


def test_run_once_rejects_over_budget_plan_without_ack_or_partial_tasks(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    plan_path = repo / ".orch/plans/P-0001.md"
    plan_path.write_text(
        "# Plan\n\n"
        f"```yaml\n{yaml.safe_dump(load_task('T-0001'), sort_keys=False)}```\n\n"
        f"```yaml\n{yaml.safe_dump(load_task('T-0002'), sort_keys=False)}```",
        encoding="utf-8",
    )
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "planned", "plan_path": ".orch/plans/P-0001.md"})

    result = low_task_budget_runtime(repo).run_once()

    assert result.kind == "budget_exceeded"
    assert "max_tasks_per_request=1" in result.message
    assert TaskStore(repo).list_tasks("pending") == []
    assert inbox.read_next("orchestrator") is not None
    log_files = list((repo / ".orch/logs/orchestrator").glob("*.jsonl"))
    assert len(log_files) == 1
    assert "budget_exceeded" in log_files[0].read_text(encoding="utf-8")


def test_submit_request_budget_rejection_acks_submit_and_preserves_planned_handoff(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    plan_path = repo / ".orch/plans/P-0001.md"
    plan_path.write_text(
        "# Plan\n\n"
        f"```yaml\n{yaml.safe_dump(load_task('T-0001'), sort_keys=False)}```\n\n"
        f"```yaml\n{yaml.safe_dump(load_task('T-0002'), sort_keys=False)}```",
        encoding="utf-8",
    )
    inbox = Inbox(repo)
    inbox.post(
        "orchestrator",
        {"action": "submit_request", "request_path": ".orch/requests/R.md"},
    )
    wrapper = FakePlannerWrapper(
        planner_result(
            repo,
            handoff={"action": "planned", "plan_path": ".orch/plans/P-0001.md"},
        )
    )

    result = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        budget_config=BudgetConfig(max_tasks_per_request=1, max_wall_clock_minutes=60),
        model_wrapper=wrapper,
    ).run_once()

    assert result.kind == "budget_exceeded"
    assert TaskStore(repo).list_tasks("pending") == []
    planned = inbox.read_next("orchestrator")
    assert planned is not None
    assert planned.body["action"] == "planned"


def test_run_once_leaves_invalid_planner_handoff_unacked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "planned"})

    try:
        runtime(repo).run_once()
    except ValueError as exc:
        assert "plan_path" in str(exc)
    else:
        raise AssertionError("run_once should reject malformed planner handoff")

    assert inbox.read_next("orchestrator") is not None


def test_run_once_dispatches_worker_completion_to_critic(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    task["owned_files"] = ["feature.txt"]
    store = TaskStore(repo)
    store.write_pending(task)
    store.transition("T-0001", "active", "self_review")
    worktree = repo / ".orch/worktrees/T-0001"
    git(repo, "worktree", "add", "-b", "task/T-0001", str(worktree), "main")
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "worker_completed", "task_id": "T-0001"})

    result = runtime(repo).run_once()

    assert result.kind == "critic_dispatched"
    assert result.critic_dispatch is not None
    assert result.critic_dispatch.diff_path == repo / ".orch/patches/T-0001.diff"
    assert store.read("T-0001")["status"] == "critic_review"
    assert inbox.read_next("critic") is not None
    assert inbox.read_next("orchestrator") is None


def _make_critic_review_task(repo: Path, *, extra_notes: list[dict] | None = None) -> dict:
    task = load_task()
    task["owned_files"] = ["feature.txt"]
    task["review_notes"] = extra_notes or []
    store = TaskStore(repo)
    store.write_pending(task)
    store.transition("T-0001", "active", "critic_review")
    return task


def _make_feature_worktree(repo: Path) -> None:
    worktree = repo / ".orch/worktrees/T-0001"
    git(repo, "worktree", "add", "-b", "task/T-0001", str(worktree), "main")
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")


def test_critic_approval_triggers_integration_and_merges(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    _make_critic_review_task(repo)
    _make_feature_worktree(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "approve",
        "body": "All ACs satisfied.",
    })

    result = runtime(repo).run_once()

    assert result.kind == "merged"
    assert result.merge_result is not None
    assert result.merge_result.merged
    store = TaskStore(repo)
    assert store.read("T-0001")["status"] == "merged"
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    assert inbox.read_next("orchestrator") is None


def test_critic_approval_appends_approve_note_before_merge(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    _make_critic_review_task(repo)
    _make_feature_worktree(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "approve",
        "body": "Looks good.",
    })

    runtime(repo).run_once()

    store = TaskStore(repo)
    notes = store.read("T-0001")["review_notes"]
    assert any(n["author"] == "gemini-critic" and n["verdict"] == "approve" for n in notes)


def test_critic_request_changes_routes_back_to_worker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    _make_critic_review_task(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "request_changes",
        "body": "Missing test coverage.",
    })

    result = runtime(repo).run_once()

    assert result.kind == "critic_rework_dispatched"
    store = TaskStore(repo)
    assert store.read("T-0001")["status"] == "in_progress"
    worker_msg = inbox.read_next("worker")
    assert worker_msg is not None
    assert worker_msg.body["task_id"] == "T-0001"
    notes = store.read("T-0001")["review_notes"]
    critic_notes = [n for n in notes if n["author"] == "gemini-critic"]
    assert len(critic_notes) == 1
    assert critic_notes[0]["verdict"] == "request_changes"
    assert inbox.read_next("orchestrator") is None


def test_critic_request_changes_escalates_after_max_retries(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    prior_notes = [
        {
            "author": "gemini-critic",
            "timestamp": "2024-01-01T00:00:00Z",
            "verdict": "request_changes",
            "body": "Round 1.",
        },
        {
            "author": "gemini-critic",
            "timestamp": "2024-01-02T00:00:00Z",
            "verdict": "request_changes",
            "body": "Round 2.",
        },
    ]
    _make_critic_review_task(repo, extra_notes=prior_notes)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "request_changes",
        "body": "Still not right.",
    })

    result = runtime(repo).run_once()

    assert result.kind == "escalated"
    store = TaskStore(repo)
    assert store.read("T-0001")["status"] == "blocked"
    assert inbox.read_next("worker") is None
    assert inbox.read_next("orchestrator") is None


def test_critic_reject_abandons_task(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    _make_critic_review_task(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "reject",
        "body": "Task scope is too large.",
    })

    result = runtime(repo).run_once()

    assert result.kind == "abandoned"
    store = TaskStore(repo)
    assert store.read("T-0001")["status"] == "abandoned"
    assert inbox.read_next("orchestrator") is None


def test_critic_reviewed_missing_task_id_stays_unacked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "critic_reviewed", "verdict": "approve"})

    try:
        runtime(repo).run_once()
    except ValueError as exc:
        assert "task_id" in str(exc)
    else:
        raise AssertionError("run_once should raise for missing task_id")

    assert inbox.read_next("orchestrator") is not None


def test_critic_reviewed_invalid_verdict_stays_unacked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    _make_critic_review_task(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "maybe",
    })

    try:
        runtime(repo).run_once()
    except ValueError as exc:
        assert "verdict" in str(exc)
    else:
        raise AssertionError("run_once should raise for invalid verdict")

    assert inbox.read_next("orchestrator") is not None


def test_critic_approval_with_merge_conflict_routes_back_to_worker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    task["owned_files"] = ["README.md"]
    store = TaskStore(repo)
    store.write_pending(task)
    store.transition("T-0001", "active", "critic_review")
    worktree = repo / ".orch/worktrees/T-0001"
    git(repo, "worktree", "add", "-b", "task/T-0001", str(worktree), "main")
    (worktree / "README.md").write_text("# Worker\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "worker readme")
    # Diverge main so the patch won't apply cleanly.
    (repo / "README.md").write_text("# Main\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "main readme")
    inbox = Inbox(repo)
    inbox.post("orchestrator", {
        "action": "critic_reviewed",
        "task_id": "T-0001",
        "verdict": "approve",
        "body": "Approved.",
    })

    result = runtime(repo).run_once()

    assert result.kind == "merge_failed_reworking"
    assert result.merge_result is not None
    assert result.merge_result.status == "conflict"
    assert store.read("T-0001")["status"] == "in_progress"
    worker_msg = inbox.read_next("worker")
    assert worker_msg is not None
    assert worker_msg.body["task_id"] == "T-0001"
    assert inbox.read_next("orchestrator") is None


def test_mocked_worker_critic_merge_path_needs_no_manual_wrapper(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    task["owned_files"] = ["feature.txt"]
    TaskStore(repo).write_pending(task)
    wrapper = SequentialWrapper(
        repo,
        [
            agent_result(
                repo,
                role="codex-worker",
                handoff={"action": "worker_completed", "task_id": "T-0001"},
            ),
            agent_result(
                repo,
                role="gemini-critic",
                handoff={
                    "action": "critic_reviewed",
                    "task_id": "T-0001",
                    "verdict": "approve",
                    "body": "Looks good.",
                },
            ),
        ]
    )
    orch = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
        ),
        model_wrapper=wrapper,
    )

    assert orch.run_once().kind == "dispatched"
    worktree = repo / ".orch/worktrees/T-0001"
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")

    assert orch.run_once().kind == "agent_ran"
    assert orch.run_once().kind == "critic_dispatched"
    assert orch.run_once().kind == "agent_ran"
    result = orch.run_once()

    assert result.kind == "merged"
    assert TaskStore(repo).read("T-0001")["status"] == "merged"
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "hello\n"


def test_startup_reconcile_clears_stale_pid_and_counts_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())
    store.transition("T-0001", "active", "in_progress")
    (repo / ".orch/locks/orchestrator.pid").write_text("999999999\n", encoding="utf-8")
    Inbox(repo).post("orchestrator", {"action": "submit_request"})

    result = runtime(repo).startup_reconcile()

    assert result.cleared_stale_pid
    assert result.active_tasks == 1
    assert result.orchestrator_messages == 1
    assert result.worktrees == 1
    assert not (repo / ".orch/locks/orchestrator.pid").exists()


def test_run_loop_drains_work_until_idle_and_cleans_pid(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "unknown"})
    seen: list[str] = []

    result = runtime(repo).run(
        sleep=lambda seconds: None,
        on_result=lambda event: seen.append(event.kind),
        max_idle_cycles=1,
    )

    assert result.kind == "idle"
    assert result.iterations == 2
    assert seen == ["ignored_message", "idle"]
    assert not (repo / ".orch/locks/orchestrator.pid").exists()
    log_files = list((repo / ".orch/logs/orchestrator").glob("*.jsonl"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "run_started" in log_text
    assert "run_shutdown" in log_text


def test_run_loop_stops_on_stop_request_and_leaves_inbox_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "unknown"})
    calls = 0

    def stop_requested() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    result = runtime(repo).run(
        stop_requested=stop_requested,
        sleep=lambda seconds: None,
    )

    assert result.kind == "stopped"
    assert result.iterations == 1
    assert inbox.read_next("orchestrator") is None
    assert not (repo / ".orch/locks/orchestrator.pid").exists()


def test_run_loop_exits_when_wall_clock_budget_is_hit_with_work_remaining(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    TaskStore(repo).write_pending(load_task())
    ticks = iter([0.0, 61.0])
    seen: list[str] = []
    orch = OrchestraRuntime(
        root=repo,
        runtime_config=RuntimeConfig(
            max_workers=1,
            default_timeout_seconds=60,
            max_retries=2,
            poll_interval_seconds=1,
        ),
        budget_config=BudgetConfig(max_tasks_per_request=5, max_wall_clock_minutes=1),
    )

    result = orch.run(
        sleep=lambda seconds: None,
        monotonic=lambda: next(ticks),
        on_result=lambda event: seen.append(event.kind),
    )

    assert result.kind == "budget_exceeded"
    assert result.iterations == 1
    assert seen == ["budget_exceeded"]
    assert TaskStore(repo).read("T-0001")["status"] == "pending"
    assert not (repo / ".orch/locks/orchestrator.pid").exists()
