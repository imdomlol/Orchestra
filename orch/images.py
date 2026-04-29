"""Docker image build helpers for the command sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from orch.config import OrchestraConfig, SandboxConfig, load_config


@dataclass(frozen=True)
class ImageBuildResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


class SandboxImageBuilder:
    """Build the project-specific Docker image configured for sandbox runs."""

    def __init__(self, root: Path = Path("."), *, sandbox: SandboxConfig) -> None:
        self.root = root.resolve()
        self.sandbox = sandbox

    @classmethod
    def from_config(
        cls,
        *,
        root: Path = Path("."),
        config: OrchestraConfig | None = None,
    ) -> "SandboxImageBuilder":
        resolved_root = root.resolve()
        loaded = config or load_config(resolved_root / ".orch" / "config")
        return cls(resolved_root, sandbox=loaded.sandbox)

    def build_argv(self, *, no_cache: bool = False, pull: bool = False) -> tuple[str, ...]:
        dockerfile = self._inside_root(self.sandbox.dockerfile, "sandbox.dockerfile")
        context = self._inside_root(self.sandbox.build_context, "sandbox.build_context")
        argv = [
            self.sandbox.docker,
            "build",
            "--tag",
            self.sandbox.image,
            "--file",
            str(dockerfile),
        ]
        if no_cache:
            argv.append("--no-cache")
        if pull:
            argv.append("--pull")
        argv.append(str(context))
        return tuple(argv)

    def build(self, *, no_cache: bool = False, pull: bool = False) -> ImageBuildResult:
        argv = self.build_argv(no_cache=no_cache, pull=pull)
        completed = subprocess.run(
            argv,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return ImageBuildResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _inside_root(self, configured_path: str, label: str) -> Path:
        path = Path(configured_path)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"{label} is outside repo root: {configured_path}") from exc
        return path
