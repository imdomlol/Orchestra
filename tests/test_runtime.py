from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import yaml

from orch.config import RuntimeConfig
from orch.inbox import Inbox
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
        ),
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


def test_run_once_dispatches_ready_task_after_submit_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())
    inbox = Inbox(repo)
    inbox.post("orchestrator", {"action": "submit_request", "request_path": ".orch/requests/R.md"})

    result = runtime(repo).run_once()

    assert result.kind == "dispatched"
    assert result.dispatch is not None
    assert result.dispatch.task_id == "T-0001"
    assert store.read("T-0001")["status"] == "in_progress"
    assert inbox.read_next("worker") is not None
    assert inbox.read_next("orchestrator") is None


def test_run_once_dispatches_ready_task_when_no_inbox_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())

    result = runtime(repo).run_once()

    assert result.kind == "dispatched"
    assert store.read("T-0001")["status"] == "in_progress"


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
