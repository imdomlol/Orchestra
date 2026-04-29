from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import yaml

from orch.config import RuntimeConfig
from orch.dispatcher import Dispatcher, globs_may_overlap
from orch.inbox import Inbox
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


def runtime(max_workers: int = 1) -> RuntimeConfig:
    return RuntimeConfig(
        max_workers=max_workers,
        default_timeout_seconds=60,
        max_retries=2,
    )


def test_dispatch_next_claims_task_creates_worktree_and_posts_worker_message(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    store.write_pending(load_task())

    result = Dispatcher(root=repo, runtime=runtime()).dispatch_next()

    assert result is not None
    assert result.task_id == "T-0001"
    assert result.task_path == repo / ".orch/tasks/active/T-0001.yaml"
    assert result.worktree_path == repo / ".orch/worktrees/T-0001"
    assert store.read("T-0001")["status"] == "in_progress"
    message = Inbox(repo).read_next("worker")
    assert message is not None
    assert message.body == {
        "task_id": "T-0001",
        "task_yaml_path": ".orch/tasks/active/T-0001.yaml",
        "worktree_path": ".orch/worktrees/T-0001",
        "role": "worker",
    }


def test_dispatch_skips_task_until_dependencies_are_merged(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    dependency = load_task("T-0001")
    task = load_task("T-0002")
    task["dependencies"] = ["T-0001"]
    store.write_pending(dependency)
    store.transition("T-0001", "done", "merged")
    store.write_pending(task)

    result = Dispatcher(root=repo, runtime=runtime()).dispatch_next()

    assert result is not None
    assert result.task_id == "T-0002"


def test_dispatch_waits_when_dependency_is_not_merged(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    task = load_task()
    task["dependencies"] = ["T-9999"]
    store.write_pending(task)

    result = Dispatcher(root=repo, runtime=runtime()).dispatch_next()

    assert result is None
    assert store.read("T-0001")["status"] == "pending"


def test_dispatch_respects_max_workers(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    active = load_task("T-0001")
    pending = load_task("T-0002")
    pending["owned_files"] = ["tests/**"]
    store.write_pending(active)
    store.transition("T-0001", "active", "in_progress")
    store.write_pending(pending)

    result = Dispatcher(root=repo, runtime=runtime(max_workers=1)).dispatch_next()

    assert result is None
    assert store.read("T-0002")["status"] == "pending"


def test_dispatch_skips_owned_file_collisions(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    store = TaskStore(repo)
    active = load_task("T-0001")
    first_pending = load_task("T-0002")
    second_pending = load_task("T-0003")
    active["owned_files"] = ["orch/**"]
    first_pending["owned_files"] = ["orch/dispatcher.py"]
    second_pending["owned_files"] = ["tests/**"]
    store.write_pending(active)
    store.transition("T-0001", "active", "in_progress")
    store.write_pending(first_pending)
    store.write_pending(second_pending)

    result = Dispatcher(root=repo, runtime=runtime(max_workers=2)).dispatch_next()

    assert result is not None
    assert result.task_id == "T-0003"
    assert store.read("T-0002")["status"] == "pending"


def test_globs_may_overlap_conservatively() -> None:
    assert globs_may_overlap("orch/**", "orch/dispatcher.py")
    assert globs_may_overlap("docs/PLAN.md", "docs/PLAN.md")
    assert not globs_may_overlap("orch/**", "tests/**")
