from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import orch.cli as cli
from orch.cli import main
from orch.doctor import DoctorCheck, DoctorReport
from orch.inbox import Inbox
from orch.runtime import RunLoopResult, RunOnceResult


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
    assert "Idle" in capsys.readouterr().out


def test_run_cli_starts_continuous_loop(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    calls: list[dict] = []

    class FakeRuntime:
        @classmethod
        def from_config(cls, *, root: Path, on_progress=None, on_confirm=None, model_stderr_sink=None):
            assert root == repo
            return cls()

        def run(self, *, stop_requested, on_result):
            calls.append({"stop_requested": stop_requested, "on_result": on_result})
            on_result(RunOnceResult(kind="idle", message="no actionable work"))
            return RunLoopResult(
                kind="stopped",
                message="run loop stopped",
                iterations=1,
                last_result=None,
            )

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "run"])

    assert code == 0
    assert len(calls) == 1
    assert "Idle" in capsys.readouterr().out


def test_image_build_prints_configured_docker_command(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    code = main(["--root", str(repo), "image", "build", "--print", "--pull"])

    assert code == 0
    output = capsys.readouterr().out.strip()
    assert "docker build" in output
    assert "--tag orchestra-sandbox:py3.12" in output
    assert "--pull" in output
    assert str(repo / "docker/orchestra-sandbox.Dockerfile") in output


def test_doctor_cli_prints_checks_and_sets_exit_code(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    class FakeDoctor:
        @classmethod
        def from_config(cls, *, root: Path):
            assert root == repo
            return cls()

        def run(self):
            return DoctorReport(
                (
                    DoctorCheck("gemini CLI", True, "gemini 1.0"),
                    DoctorCheck("docker daemon", False, "daemon unavailable"),
                )
            )

    monkeypatch.setattr(cli, "Doctor", FakeDoctor)

    code = main(["--root", str(repo), "doctor"])

    output = capsys.readouterr().out
    assert code == 1
    assert "PASS gemini CLI: gemini 1.0" in output
    assert "FAIL docker daemon: daemon unavailable" in output
