"""Subprocess execution with command allowlisting and durable logs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Sequence
from uuid import uuid4

from orch.config import SandboxConfig


ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
LOG_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class CommandNotAllowed(ValueError):
    """Raised when a requested command is not in a task allowlist."""


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    cwd: Path
    returncode: int | None
    stdout_path: Path
    stderr_path: Path
    timed_out: bool
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SubprocessRunner:
    """Run external role processes and write stdout/stderr under `.orch/logs`."""

    def __init__(self, root: Path = Path(".")) -> None:
        self.root = root.resolve()
        self.logs_root = self.root / ".orch" / "logs"

    def run(
        self,
        argv: Sequence[str],
        *,
        role: str,
        log_name: str | None = None,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout_seconds: int,
    ) -> ProcessResult:
        if not argv:
            raise ValueError("argv must not be empty")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")

        working_dir = (cwd or self.root).resolve()
        self._validate_inside_root(working_dir, "cwd")
        stdout_path, stderr_path = self._log_paths(role, log_name)

        started = time.monotonic()
        timed_out = False
        returncode: int | None
        try:
            completed = subprocess.run(
                list(argv),
                cwd=working_dir,
                input=stdin,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = None
            stdout = _coerce_output(exc.stdout)
            stderr = _coerce_output(exc.stderr)
            timeout_note = f"\n[orchestra] timed out after {timeout_seconds} seconds\n"
            stderr = f"{stderr}{timeout_note}" if stderr else timeout_note.lstrip()

        duration = time.monotonic() - started
        self._write_log(stdout_path, stdout)
        self._write_log(stderr_path, stderr)
        return ProcessResult(
            argv=tuple(argv),
            cwd=working_dir,
            returncode=returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timed_out=timed_out,
            duration_seconds=duration,
        )

    def run_allowed(
        self,
        command: str,
        *,
        allowed_commands: Sequence[str],
        role: str,
        log_name: str | None = None,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> ProcessResult:
        if command not in allowed_commands:
            raise CommandNotAllowed(f"command is not allowed: {command}")
        argv = shlex.split(command)
        if not argv:
            raise ValueError("command must not be empty")
        return self.run(
            argv,
            role=role,
            log_name=log_name,
            cwd=cwd,
            stdin=None,
            timeout_seconds=timeout_seconds,
        )

    def _log_paths(self, role: str, log_name: str | None) -> tuple[Path, Path]:
        self._validate_role(role)
        name = log_name or self._default_log_name()
        if not LOG_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid log name: {name}")

        role_dir = self.logs_root / role
        role_dir.mkdir(parents=True, exist_ok=True)
        return role_dir / f"{name}.stdout", role_dir / f"{name}.stderr"

    def _write_log(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        self._fsync_dir(path.parent)

    def _validate_role(self, role: str) -> None:
        if not ROLE_RE.fullmatch(role):
            raise ValueError(f"invalid log role: {role}")

    def _validate_inside_root(self, path: Path, label: str) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"{label} is outside repo root: {path}") from exc

    def _default_log_name(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}-{os.getpid()}-{uuid4().hex}"

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class DockerRunner(SubprocessRunner):
    """Run allowed commands inside a Docker container sandbox."""

    def __init__(
        self,
        root: Path = Path("."),
        *,
        sandbox: SandboxConfig,
    ) -> None:
        super().__init__(root)
        self.sandbox = sandbox

    def run(
        self,
        argv: Sequence[str],
        *,
        role: str,
        log_name: str | None = None,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout_seconds: int,
    ) -> ProcessResult:
        working_dir = (cwd or self.root).resolve()
        self._validate_inside_root(working_dir, "cwd")
        docker_argv = self.build_docker_argv(argv, cwd=working_dir)
        return super().run(
            docker_argv,
            role=role,
            log_name=log_name,
            cwd=self.root,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
        )

    def build_docker_argv(self, argv: Sequence[str], *, cwd: Path) -> tuple[str, ...]:
        if not argv:
            raise ValueError("argv must not be empty")
        self._validate_inside_root(cwd, "cwd")

        container_root = self.sandbox.workdir.rstrip("/") or "/workspace"
        relative_cwd = cwd.relative_to(self.root)
        container_cwd = _container_path(container_root, relative_cwd)
        command = shlex.join(argv)
        mount_args = self._mount_args(cwd, container_root)

        return (
            self.sandbox.docker,
            "run",
            "--rm",
            "--network",
            self.sandbox.network,
            "--workdir",
            container_cwd,
            *mount_args,
            self.sandbox.image,
            "sh",
            "-lc",
            command,
        )

    def _mount_args(self, cwd: Path, container_root: str) -> tuple[str, ...]:
        root_mount = f"{self.root}:{container_root}:ro"
        if cwd == self.root:
            return ("--volume", f"{self.root}:{container_root}:rw")

        relative_cwd = cwd.relative_to(self.root)
        cwd_mount = f"{cwd}:{_container_path(container_root, relative_cwd)}:rw"
        return ("--volume", root_mount, "--volume", cwd_mount)


def make_runner(root: Path = Path("."), *, sandbox: SandboxConfig) -> SubprocessRunner:
    if sandbox.mode == "docker":
        return DockerRunner(root, sandbox=sandbox)
    raise ValueError(f"unsupported sandbox mode: {sandbox.mode}")


def _coerce_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _container_path(container_root: str, relative: Path) -> str:
    if str(relative) == ".":
        return container_root
    return f"{container_root}/{relative.as_posix()}"
