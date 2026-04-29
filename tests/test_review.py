from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import yaml

from orch.inbox import Inbox
from orch.review import ReviewDispatcher
from orch.task_store import TaskStore
from orch.worktree import WorktreeManager


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
        ".orch/patches",
        ".orch/schemas",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy(".orch/schemas/task.schema.json", repo / ".orch/schemas/task.schema.json")


def load_task(task_id: str = "T-0001") -> dict:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text(encoding="utf-8"))
    task["id"] = task_id
    task["branch"] = f"task/{task_id}"
    task["worktree_path"] = f".orch/worktrees/{task_id}"
    task["owned_files"] = ["feature.txt"]
    return task


def create_active_task(repo: Path, task: dict) -> Path:
    store = TaskStore(repo)
    store.write_pending(task)
    store.transition(task["id"], "active", "self_review")
    return WorktreeManager(repo).create(task["id"]).path


def test_dispatch_to_critic_exports_diff_and_posts_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    worktree = create_active_task(repo, task)
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")

    result = ReviewDispatcher(repo).dispatch_to_critic("T-0001")

    assert result.diff_path == repo / ".orch/patches/T-0001.diff"
    assert "+hello" in result.diff_path.read_text(encoding="utf-8")
    assert result.task_path == repo / ".orch/tasks/active/T-0001.yaml"
    assert TaskStore(repo).read("T-0001")["status"] == "critic_review"
    message = Inbox(repo).read_next("critic")
    assert message is not None
    assert message.body == {
        "task_id": "T-0001",
        "task_yaml_path": ".orch/tasks/active/T-0001.yaml",
        "diff_path": ".orch/patches/T-0001.diff",
        "policy_path": ".orch/config/policies.toml",
        "role": "critic",
    }
