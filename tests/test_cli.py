from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from orch.cli import main
from orch.inbox import Inbox


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


def copy_configured_layout(repo: Path) -> None:
    for directory in [
        ".orch/tasks/pending",
        ".orch/tasks/active",
        ".orch/tasks/done",
        ".orch/locks",
        ".orch/schemas",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copytree(".orch/config", repo / ".orch/config")
    shutil.copy(".orch/schemas/task.schema.json", repo / ".orch/schemas/task.schema.json")


def test_submit_cli_records_request(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    code = main(["--root", str(repo), "submit", "Ship it"])

    assert code == 0
    output = capsys.readouterr().out.strip()
    assert output.startswith(str(repo / ".orch/requests/R-"))
    assert Inbox(repo).read_next("orchestrator") is not None


def test_run_once_cli_reports_idle(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    code = main(["--root", str(repo), "run", "--once"])

    assert code == 0
    assert '"kind": "idle"' in capsys.readouterr().out
