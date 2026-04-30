from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from orch.doctor import Doctor, REQUIRED_ORCH_DIRS


def copy_preflight_files(repo: Path) -> None:
    shutil.copytree(".orch/config", repo / ".orch/config")
    shutil.copytree(".orch/schemas", repo / ".orch/schemas")
    shutil.copytree("examples", repo / "examples")
    shutil.copytree("docker", repo / "docker")
    for directory in REQUIRED_ORCH_DIRS:
        (repo / directory).mkdir(parents=True, exist_ok=True)


def completed(
    argv: tuple[str, ...],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_doctor_passes_when_environment_is_ready(tmp_path: Path) -> None:
    copy_preflight_files(tmp_path)

    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ("gemini", "--version"):
            return completed(argv, stdout="gemini 1.2.3\n")
        if argv[:2] == ("codex", "--version"):
            return completed(argv, stdout="codex 5.5\n")
        if argv[:2] == ("docker", "version"):
            return completed(argv, stdout="25.0.0\n")
        if argv[:3] == ("docker", "image", "inspect"):
            return completed(argv, stdout="[]\n")
        if argv == ("git", "config", "user.name"):
            return completed(argv, stdout="Test User\n")
        if argv == ("git", "config", "user.email"):
            return completed(argv, stdout="test@example.local\n")
        raise AssertionError(f"unexpected command: {argv}")

    report = Doctor(
        tmp_path,
        runner=runner,
        which=lambda executable: f"/bin/{executable}",
    ).run()

    assert report.passed
    assert any(line.startswith("PASS gemini CLI:") for line in report.lines())
    assert any(line == "PASS sandbox image: orchestra-sandbox:py3.12 present" for line in report.lines())


def test_doctor_accepts_missing_image_when_build_inputs_exist(tmp_path: Path) -> None:
    copy_preflight_files(tmp_path)

    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ("docker", "image", "inspect"):
            return completed(argv, returncode=1, stderr="No such image\n")
        if argv == ("git", "config", "user.name"):
            return completed(argv, stdout="Test User\n")
        if argv == ("git", "config", "user.email"):
            return completed(argv, stdout="test@example.local\n")
        return completed(argv, stdout="ok\n")

    report = Doctor(
        tmp_path,
        runner=runner,
        which=lambda executable: f"/bin/{executable}",
    ).run()

    assert report.passed
    assert any("missing; buildable with docker build" in line for line in report.lines())


def test_doctor_reports_missing_cli_and_dirs(tmp_path: Path) -> None:
    copy_preflight_files(tmp_path)
    shutil.rmtree(tmp_path / ".orch" / "tasks")

    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv == ("git", "config", "user.name"):
            return completed(argv, stdout="Test User\n")
        if argv == ("git", "config", "user.email"):
            return completed(argv, stdout="test@example.local\n")
        return completed(argv, stdout="ok\n")

    def which(executable: str) -> str | None:
        if executable == "gemini":
            return None
        return f"/bin/{executable}"

    report = Doctor(tmp_path, runner=runner, which=which).run()

    assert not report.passed
    lines = report.lines()
    assert any(line == "FAIL gemini CLI: 'gemini' is not on PATH" for line in lines)
    assert any(line.startswith("FAIL orch directories: missing .orch/tasks/pending") for line in lines)


def test_doctor_reports_invalid_git_identity(tmp_path: Path) -> None:
    copy_preflight_files(tmp_path)

    def runner(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv == ("git", "config", "user.name"):
            return completed(argv, returncode=1)
        if argv == ("git", "config", "user.email"):
            return completed(argv, stdout="")
        return completed(argv, stdout="ok\n")

    report = Doctor(
        tmp_path,
        runner=runner,
        which=lambda executable: f"/bin/{executable}",
    ).run()

    assert not report.passed
    assert "FAIL git identity: missing user.name, user.email" in report.lines()
