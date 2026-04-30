"""Environment preflight checks for first Orchestra runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Callable

import jsonschema
import yaml

from orch.config import OrchestraConfig, load_config
from orch.images import SandboxImageBuilder


CommandRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


REQUIRED_ORCH_DIRS = (
    ".orch/config",
    ".orch/config/prompts",
    ".orch/schemas",
    ".orch/requests",
    ".orch/plans",
    ".orch/tasks/pending",
    ".orch/tasks/active",
    ".orch/tasks/done",
    ".orch/worktrees",
    ".orch/logs/orchestrator",
    ".orch/logs/planner",
    ".orch/logs/critic",
    ".orch/logs/workers",
    ".orch/logs/integrator",
    ".orch/patches",
    ".orch/summaries",
    ".orch/locks",
    ".orch/inbox/orchestrator",
    ".orch/inbox/worker",
    ".orch/inbox/critic",
    ".orch/inbox/integrator",
)


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def lines(self) -> list[str]:
        return [
            f"{'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}"
            for check in self.checks
        ]


class Doctor:
    """Run deterministic local preflight checks for Orchestra."""

    def __init__(
        self,
        root: Path = Path("."),
        *,
        config: OrchestraConfig | None = None,
        runner: CommandRunner | None = None,
        which: Which | None = None,
    ) -> None:
        self.root = root.resolve()
        self.config = config or load_config(self.root / ".orch" / "config")
        self.runner = runner or _run
        self.which = which or shutil.which

    @classmethod
    def from_config(cls, *, root: Path = Path(".")) -> "Doctor":
        return cls(root)

    def run(self) -> DoctorReport:
        checks = [
            self._cli_check("gemini CLI", self.config.cli.gemini),
            self._cli_check("codex CLI", self.config.cli.codex),
            self._docker_check(),
            self._sandbox_image_check(),
            self._git_identity_check(),
            self._task_schema_check(),
            self._required_dirs_check(),
        ]
        return DoctorReport(tuple(checks))

    def _cli_check(self, name: str, command: str) -> DoctorCheck:
        try:
            argv = _split_command(command)
            executable = argv[0]
            if self.which(executable) is None:
                return DoctorCheck(name, False, f"{executable!r} is not on PATH")
            result = self.runner((*argv, "--version"), self.root)
        except Exception as exc:  # noqa: BLE001 - preflight reports failures.
            return DoctorCheck(name, False, str(exc))
        if result.returncode != 0:
            return DoctorCheck(name, False, _command_failure_detail(result))
        version = _first_output_line(result) or "version command succeeded"
        return DoctorCheck(name, True, version)

    def _docker_check(self) -> DoctorCheck:
        docker = self.config.sandbox.docker
        if self.which(docker) is None:
            return DoctorCheck("docker daemon", False, f"{docker!r} is not on PATH")
        try:
            result = self.runner((docker, "version", "--format", "{{.Server.Version}}"), self.root)
        except Exception as exc:  # noqa: BLE001 - preflight reports failures.
            return DoctorCheck("docker daemon", False, str(exc))
        if result.returncode != 0:
            return DoctorCheck("docker daemon", False, _command_failure_detail(result))
        version = _first_output_line(result) or "daemon reachable"
        return DoctorCheck("docker daemon", True, version)

    def _sandbox_image_check(self) -> DoctorCheck:
        if self.which(self.config.sandbox.docker) is None:
            return DoctorCheck(
                "sandbox image",
                False,
                f"cannot inspect image because {self.config.sandbox.docker!r} is not on PATH",
            )
        try:
            result = self.runner(
                (self.config.sandbox.docker, "image", "inspect", self.config.sandbox.image),
                self.root,
            )
        except Exception as exc:  # noqa: BLE001 - preflight reports failures.
            return DoctorCheck("sandbox image", False, str(exc))
        if result.returncode == 0:
            return DoctorCheck("sandbox image", True, f"{self.config.sandbox.image} present")

        builder = SandboxImageBuilder.from_config(root=self.root, config=self.config)
        try:
            argv = builder.build_argv()
        except Exception as exc:  # noqa: BLE001 - invalid config is a doctor failure.
            return DoctorCheck("sandbox image", False, str(exc))
        dockerfile = Path(argv[argv.index("--file") + 1])
        context = Path(argv[-1])
        if dockerfile.is_file() and context.is_dir():
            return DoctorCheck(
                "sandbox image",
                True,
                f"{self.config.sandbox.image} missing; buildable with {shlex.join(argv)}",
            )
        missing = []
        if not dockerfile.is_file():
            missing.append(str(dockerfile))
        if not context.is_dir():
            missing.append(str(context))
        return DoctorCheck("sandbox image", False, "missing build inputs: " + ", ".join(missing))

    def _git_identity_check(self) -> DoctorCheck:
        if self.which("git") is None:
            return DoctorCheck("git identity", False, "'git' is not on PATH")
        try:
            name = self.runner(("git", "config", "user.name"), self.root)
            email = self.runner(("git", "config", "user.email"), self.root)
        except Exception as exc:  # noqa: BLE001 - preflight reports failures.
            return DoctorCheck("git identity", False, str(exc))
        missing = []
        if name.returncode != 0 or not name.stdout.strip():
            missing.append("user.name")
        if email.returncode != 0 or not email.stdout.strip():
            missing.append("user.email")
        if missing:
            return DoctorCheck("git identity", False, "missing " + ", ".join(missing))
        return DoctorCheck("git identity", True, f"{name.stdout.strip()} <{email.stdout.strip()}>")

    def _task_schema_check(self) -> DoctorCheck:
        schema_path = self.root / ".orch" / "schemas" / "task.schema.json"
        example_path = self.root / "examples" / "task.example.yaml"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            task = yaml.safe_load(example_path.read_text(encoding="utf-8"))
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
            validator = validator_cls(schema, format_checker=jsonschema.FormatChecker())
            errors = sorted(validator.iter_errors(task), key=lambda error: error.path)
        except Exception as exc:  # noqa: BLE001 - preflight reports failures.
            return DoctorCheck("task schema", False, str(exc))
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.absolute_path) or "<root>"
            return DoctorCheck("task schema", False, f"{location}: {first.message}")
        return DoctorCheck("task schema", True, "examples/task.example.yaml validates")

    def _required_dirs_check(self) -> DoctorCheck:
        missing = [path for path in REQUIRED_ORCH_DIRS if not (self.root / path).is_dir()]
        if missing:
            return DoctorCheck("orch directories", False, "missing " + ", ".join(missing))
        return DoctorCheck("orch directories", True, f"{len(REQUIRED_ORCH_DIRS)} required dirs exist")


def _split_command(command: str) -> tuple[str, ...]:
    argv = tuple(shlex.split(command))
    if not argv:
        raise ValueError("empty CLI command")
    return argv


def _run(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _first_output_line(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0].strip() if output else ""


def _command_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = _first_output_line(result)
    if detail:
        return detail
    return f"command exited {result.returncode}"
