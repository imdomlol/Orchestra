from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

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


def test_create_worktree_for_task(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = WorktreeManager(repo)

    info = manager.create("T-0001")

    assert info.branch == "task/T-0001"
    assert info.path.exists()
    assert (info.path / "README.md").exists()


def test_rejects_invalid_task_id(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = WorktreeManager(repo)

    with pytest.raises(ValueError, match="invalid task id"):
        manager.create("../bad")


def test_remove_refuses_dirty_worktree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = WorktreeManager(repo)
    info = manager.create("T-0001")
    (info.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="dirty worktree"):
        manager.remove("T-0001")


def test_remove_refuses_unmerged_commits(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    manager = WorktreeManager(repo)
    info = manager.create("T-0001")
    (info.path / "change.txt").write_text("change\n", encoding="utf-8")
    git(info.path, "add", "change.txt")
    git(info.path, "commit", "-m", "task change")

    with pytest.raises(RuntimeError, match="unmerged branch"):
        manager.remove("T-0001")
