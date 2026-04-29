from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import yaml

from orch.merge import MergeDriver
from orch.task_store import TaskStore
from orch.worktree import WorktreeManager


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=check,
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
        ".orch/patches",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy(".orch/schemas/task.schema.json", repo / ".orch/schemas/task.schema.json")


def load_task(task_id: str = "T-0001") -> dict:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text(encoding="utf-8"))
    task["id"] = task_id
    task["branch"] = f"task/{task_id}"
    task["worktree_path"] = f".orch/worktrees/{task_id}"
    task["owned_files"] = ["feature.txt", "README.md"]
    task["allowed_commands"] = []
    return task


def create_active_task(repo: Path, task: dict) -> Path:
    store = TaskStore(repo)
    store.write_pending(task)
    store.transition(task["id"], "active", "integration_review")
    info = WorktreeManager(repo).create(task["id"])
    return info.path


def test_merge_task_exports_patch_runs_checks_and_merges(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    worktree = create_active_task(repo, task)
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")
    check = f"{sys.executable} -c \"from pathlib import Path; assert Path('feature.txt').exists()\""

    result = MergeDriver(root=repo, check_commands=[check], check_timeout_seconds=5).merge_task(
        "T-0001"
    )

    assert result.merged
    assert (repo / ".orch/patches/T-0001.patch").exists()
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    assert (repo / ".orch/tasks/done/T-0001.yaml").exists()
    assert not (repo / ".orch/worktrees/T-0001").exists()
    assert not (repo / ".orch/worktrees/_integration").exists()
    assert git(repo, "branch", "--list", "task/T-0001").stdout == ""
    assert git(repo, "branch", "--list", "integrate/T-0001").stdout == ""
    assert result.check_results[0].succeeded


def test_merge_task_returns_checks_failed_without_merging(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    worktree = create_active_task(repo, task)
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")
    check = f"{sys.executable} -c \"raise SystemExit(7)\""

    result = MergeDriver(root=repo, check_commands=[check], check_timeout_seconds=5).merge_task(
        "T-0001"
    )

    assert result.status == "checks_failed"
    assert not (repo / "feature.txt").exists()
    assert (repo / ".orch/tasks/active/T-0001.yaml").exists()
    assert not (repo / ".orch/worktrees/_integration").exists()
    assert git(repo, "branch", "--list", "integrate/T-0001").stdout == ""
    task_after = TaskStore(repo).read("T-0001")
    assert task_after["review_notes"][-1]["author"] == "codex-integrator"
    assert task_after["review_notes"][-1]["verdict"] == "request_changes"


def test_merge_task_returns_conflict_when_patch_does_not_apply(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    copy_runtime_layout(repo)
    task = load_task()
    worktree = create_active_task(repo, task)
    (worktree / "README.md").write_text("# Worker\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "worker readme")
    (repo / "README.md").write_text("# Main\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "main readme")

    result = MergeDriver(root=repo).merge_task("T-0001")

    assert result.status == "conflict"
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Main\n"
    assert (repo / ".orch/tasks/active/T-0001.yaml").exists()
    assert not (repo / ".orch/worktrees/_integration").exists()
    assert git(repo, "branch", "--list", "integrate/T-0001").stdout == ""
    assert "Patch did not apply" in TaskStore(repo).read("T-0001")["review_notes"][-1]["body"]
