from __future__ import annotations

from pathlib import Path
import sys

import pytest

from orch.runner import CommandNotAllowed, SubprocessRunner


def test_run_allowed_captures_stdout_and_stderr(tmp_path: Path) -> None:
    runner = SubprocessRunner(tmp_path)
    command = (
        f"{sys.executable} -c "
        "\"import sys; print('hello'); print('warn', file=sys.stderr)\""
    )

    result = runner.run_allowed(
        command,
        allowed_commands=[command],
        role="workers",
        log_name="T-0001",
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert result.succeeded
    assert result.stdout_path == tmp_path / ".orch/logs/workers/T-0001.stdout"
    assert result.stdout_path.read_text(encoding="utf-8") == "hello\n"
    assert result.stderr_path.read_text(encoding="utf-8") == "warn\n"


def test_run_allowed_rejects_commands_not_listed(tmp_path: Path) -> None:
    runner = SubprocessRunner(tmp_path)

    with pytest.raises(CommandNotAllowed, match="not allowed"):
        runner.run_allowed(
            "pytest -q",
            allowed_commands=["pytest"],
            role="workers",
            log_name="T-0001",
            cwd=tmp_path,
            timeout_seconds=5,
        )


def test_run_records_timeout(tmp_path: Path) -> None:
    runner = SubprocessRunner(tmp_path)

    result = runner.run(
        [
            sys.executable,
            "-c",
            "import time; print('starting', flush=True); time.sleep(5)",
        ],
        role="planner",
        log_name="P-0001",
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert result.timed_out
    assert result.returncode is None
    assert "starting" in result.stdout_path.read_text(encoding="utf-8")
    assert "timed out after 1 seconds" in result.stderr_path.read_text(encoding="utf-8")


def test_run_rejects_cwd_outside_root(tmp_path: Path) -> None:
    runner = SubprocessRunner(tmp_path)

    with pytest.raises(ValueError, match="outside repo root"):
        runner.run(
            [sys.executable, "-c", "print('nope')"],
            role="planner",
            cwd=tmp_path.parent,
            timeout_seconds=5,
        )
