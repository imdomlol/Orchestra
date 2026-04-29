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
