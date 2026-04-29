"""Role wrappers for invoking external model CLIs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
from typing import Any

from orch.config import OrchestraConfig, load_config
from orch.inbox import Inbox
from orch.runner import ProcessResult, SubprocessRunner


ROLE_SPECS = {
    "gemini-planner": {
        "prompt": "gemini-planner.md",
        "cli": "gemini",
        "model": "planner",
        "log_role": "planner",
    },
    "gemini-critic": {
        "prompt": "gemini-critic.md",
        "cli": "gemini",
        "model": "critic",
        "log_role": "critic",
    },
    "codex-worker": {
        "prompt": "codex-worker.md",
        "cli": "codex",
        "model": "worker",
        "log_role": "workers",
    },
    "codex-integrator": {
        "prompt": "codex-integrator.md",
        "cli": "codex",
        "model": "integrator",
        "log_role": "integrator",
    },
}


@dataclass(frozen=True)
class WrapperResult:
    role: str
    process: ProcessResult
    handoff_path: Path | None
    handoff: dict[str, Any] | None

    @property
    def succeeded(self) -> bool:
        return self.process.succeeded and self.handoff_path is not None


class ModelWrapper:
    """Compose role prompts, run configured model CLIs, and post handoffs."""

    def __init__(
        self,
        root: Path = Path("."),
        *,
        config: OrchestraConfig | None = None,
        runner: SubprocessRunner | None = None,
        inbox: Inbox | None = None,
    ) -> None:
        self.root = root.resolve()
        self.config = config or load_config(self.root / ".orch" / "config")
        self.runner = runner or SubprocessRunner(self.root)
        self.inbox = inbox or Inbox(self.root)

    def run_role(
        self,
        role: str,
        *,
        timeout_seconds: int | None = None,
        log_name: str | None = None,
        inbox_role: str = "orchestrator",
        **context: Any,
    ) -> WrapperResult:
        spec = self._spec_for(role)
        prompt = self.build_prompt(role, context)
        cwd = self._cwd_for(context)
        process = self.runner.run(
            self._argv_for(spec),
            role=str(spec["log_role"]),
            log_name=log_name or self._default_log_name(context),
            cwd=cwd,
            stdin=prompt,
            timeout_seconds=timeout_seconds or self.config.runtime.default_timeout_seconds,
        )

        handoff = None
        handoff_path = None
        if process.stdout_path.exists():
            handoff = extract_handoff(process.stdout_path.read_text(encoding="utf-8"))
        if handoff is not None:
            handoff.setdefault("role", role)
            handoff_path = self.inbox.post(inbox_role, handoff)

        return WrapperResult(
            role=role,
            process=process,
            handoff_path=handoff_path,
            handoff=handoff,
        )

    def build_prompt(self, role: str, context: dict[str, Any]) -> str:
        spec = self._spec_for(role)
        prompt_path = self.root / ".orch" / "config" / "prompts" / str(spec["prompt"])
        role_prompt = prompt_path.read_text(encoding="utf-8")
        payload = {
            "role": role,
            "model": getattr(self.config.models, str(spec["model"])),
            "root": str(self.root),
            "context": self._stringify_paths(context),
        }
        return (
            f"{role_prompt.rstrip()}\n\n"
            "## Invocation Context\n\n"
            "Use only the artifact paths in this JSON payload for handoff.\n\n"
            f"```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```\n\n"
            "Emit the final Orchestra handoff as a single JSON object on stdout, "
            "or on a line prefixed with ORCH_HANDOFF:.\n"
        )

    def _spec_for(self, role: str) -> dict[str, str]:
        try:
            return ROLE_SPECS[role]
        except KeyError as exc:
            allowed = ", ".join(sorted(ROLE_SPECS))
            raise ValueError(f"unknown wrapper role {role!r}; expected one of: {allowed}") from exc

    def _argv_for(self, spec: dict[str, str]) -> tuple[str, ...]:
        command = getattr(self.config.cli, spec["cli"])
        argv = tuple(shlex.split(command))
        if not argv:
            raise ValueError(f"empty CLI command for {spec['cli']}")
        return argv

    def _cwd_for(self, context: dict[str, Any]) -> Path:
        worktree = context.get("worktree_path")
        if worktree is None:
            return self.root
        path = Path(str(worktree))
        if not path.is_absolute():
            path = self.root / path
        return path

    def _default_log_name(self, context: dict[str, Any]) -> str | None:
        task_id = context.get("task_id")
        if isinstance(task_id, str) and task_id:
            return task_id
        request_path = context.get("request_path")
        if request_path:
            return Path(str(request_path)).stem
        return None

    def _stringify_paths(self, context: dict[str, Any]) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Path) else value for key, value in context.items()}


def extract_handoff(output: str) -> dict[str, Any] | None:
    """Extract a JSON handoff from model stdout."""

    stripped = output.strip()
    if not stripped:
        return None
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if candidate.startswith("ORCH_HANDOFF:"):
            candidate = candidate.removeprefix("ORCH_HANDOFF:").strip()
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None
