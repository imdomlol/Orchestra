"""Role wrappers for invoking external model CLIs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from typing import Any, Callable

import yaml

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
    "claude-planner": {
        "prompt": "gemini-planner.md",
        "cli": "claude",
        "model": "orchestrator",
        "log_role": "planner",
        "headless_args": ["--print"],
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
        return self.process.succeeded and self.handoff is not None


class ModelWrapper:
    """Compose role prompts, run configured model CLIs, and post handoffs."""

    def __init__(
        self,
        root: Path = Path("."),
        *,
        config: OrchestraConfig | None = None,
        runner: SubprocessRunner | None = None,
        inbox: Inbox | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.config = config or load_config(self.root / ".orch" / "config")
        self.runner = runner or SubprocessRunner(self.root)
        self.inbox = inbox or Inbox(self.root)
        self.stderr_sink = stderr_sink

    def run_role(
        self,
        role: str,
        *,
        timeout_seconds: int | None = None,
        log_name: str | None = None,
        inbox_role: str = "orchestrator",
        post_handoff: bool = True,
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
            stderr_sink=self.stderr_sink,
        )

        stdout_text = ""
        handoff = None
        handoff_path = None
        if process.stdout_path.exists():
            stdout_text = process.stdout_path.read_text(encoding="utf-8")
            handoff = extract_handoff(stdout_text)
        if handoff is not None:
            handoff = self._prepare_planner_handoff(role, handoff, stdout_text)
            handoff.setdefault("role", role)
            if post_handoff:
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
        inlined = self._inline_artifacts(context)
        artifacts_section = ""
        if inlined:
            parts = []
            for key, content in inlined.items():
                parts.append(f"### {key}\n\n{content}")
            artifacts_section = "\n\n## Artifact Contents\n\n" + "\n\n".join(parts)
        return (
            f"{role_prompt.rstrip()}\n\n"
            "## Invocation Context\n\n"
            "Use only the artifact paths in this JSON payload for handoff.\n\n"
            f"```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```"
            f"{artifacts_section}\n\n"
            "Emit the final Orchestra handoff as a single JSON object on stdout, "
            "or on a line prefixed with ORCH_HANDOFF:.\n"
        )

    def _inline_artifacts(self, context: dict[str, Any]) -> dict[str, str]:
        """Read and inline content of any *_path context values that exist under root."""
        inlined: dict[str, str] = {}
        for key, value in context.items():
            if not key.endswith("_path"):
                continue
            path = Path(str(value))
            if not path.is_absolute():
                path = self.root / path
            if path.exists() and path.is_file():
                inlined[key] = path.read_text(encoding="utf-8")
        return inlined

    def _spec_for(self, role: str) -> dict[str, str]:
        try:
            return ROLE_SPECS[role]
        except KeyError as exc:
            allowed = ", ".join(sorted(ROLE_SPECS))
            raise ValueError(f"unknown wrapper role {role!r}; expected one of: {allowed}") from exc

    def _argv_for(self, spec: dict[str, Any]) -> tuple[str, ...]:
        command = getattr(self.config.cli, spec["cli"])
        argv = tuple(shlex.split(command))
        if not argv:
            raise ValueError(f"empty CLI command for {spec['cli']}")
        headless = spec.get("headless_args", [])
        return argv + tuple(headless)

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

    def _prepare_planner_handoff(
        self,
        role: str,
        handoff: dict[str, Any],
        stdout_text: str = "",
    ) -> dict[str, Any]:
        if not role.endswith("-planner"):
            return handoff

        prepared = dict(handoff)
        prepared.setdefault("action", "planned")
        tasks = prepared.get("tasks")
        plan_path = prepared.get("plan_path")
        plan_content = prepared.get("plan_content")
        if (
            isinstance(plan_path, str)
            and plan_path.strip()
            and not isinstance(plan_content, str)
            and not tasks
        ):
            plan_content = _extract_plan_markdown(stdout_text)
            if plan_content:
                prepared["plan_content"] = plan_content
        if isinstance(plan_path, str) and plan_path.strip() and isinstance(plan_content, str):
            resolved_plan = self._resolve_plan_artifact(plan_path)
            if resolved_plan is not None and not resolved_plan.exists():
                resolved_plan.parent.mkdir(parents=True, exist_ok=True)
                resolved_plan.write_text(plan_content, encoding="utf-8")
                prepared["plan_written"] = True
                prepared.pop("plan_write_error", None)
            if resolved_plan is not None:
                prepared.pop("plan_content", None)
        elif isinstance(tasks, list) and tasks and isinstance(plan_path, str) and plan_path.strip():
            resolved_plan = self._resolve_plan_artifact(plan_path)
            if resolved_plan is not None and not resolved_plan.exists():
                resolved_plan.parent.mkdir(parents=True, exist_ok=True)
                resolved_plan.write_text(self._render_plan_artifact(prepared), encoding="utf-8")
                prepared["plan_written"] = True
                prepared.pop("plan_write_error", None)
        prepared.pop("tasks", None)
        return prepared

    def _resolve_plan_artifact(self, plan_path: str) -> Path | None:
        path = Path(plan_path)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (self.root / path).resolve()
        plans_root = (self.root / ".orch" / "plans").resolve()
        try:
            resolved.relative_to(plans_root)
        except ValueError:
            return None
        return resolved

    def _render_plan_artifact(self, handoff: dict[str, Any]) -> str:
        title = Path(str(handoff.get("plan_path", "planner-output"))).stem
        lines = [f"# {title}", ""]

        assumptions = handoff.get("assumptions")
        if isinstance(assumptions, list) and assumptions:
            lines.extend(["## Assumptions", ""])
            lines.extend(f"- {assumption}" for assumption in assumptions)
            lines.append("")

        risks = handoff.get("risks")
        if isinstance(risks, list) and risks:
            lines.extend(["## Risks", ""])
            lines.extend(f"- {risk}" for risk in risks)
            lines.append("")

        lines.extend(["## Tasks", ""])
        for task in handoff["tasks"]:
            if not isinstance(task, dict):
                continue
            lines.append("```yaml")
            lines.append(yaml.safe_dump(task, sort_keys=False).rstrip())
            lines.append("```")
            lines.append("")
        return "\n".join(lines)


def extract_handoff(output: str) -> dict[str, Any] | None:
    """Extract a JSON handoff from model stdout."""

    stripped = output.strip()
    if not stripped:
        return None

    data = _decode_json_object(stripped)
    if data is not None:
        return data

    marker_index = stripped.rfind("ORCH_HANDOFF:")
    if marker_index != -1:
        data = _decode_first_json_object(stripped[marker_index + len("ORCH_HANDOFF:"):])
        if data is not None:
            return data

    for match in reversed(list(re.finditer(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL | re.IGNORECASE))):
        data = _decode_json_object(match.group(1))
        if data is not None:
            return data

    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if candidate.startswith("ORCH_HANDOFF:"):
            candidate = candidate.removeprefix("ORCH_HANDOFF:").strip()
        data = _decode_json_object(candidate)
        if data is not None:
            return data
    return None


def _extract_plan_markdown(output: str) -> str | None:
    marker_index = output.rfind("ORCH_HANDOFF:")
    if marker_index == -1:
        markdown = output
    else:
        remainder = output[marker_index + len("ORCH_HANDOFF:") :]
        decoder = json.JSONDecoder()
        markdown = ""
        for index, char in enumerate(remainder):
            if char != "{":
                continue
            try:
                _, end = decoder.raw_decode(remainder[index:])
            except json.JSONDecodeError:
                continue
            markdown = remainder[index + end :]
            break

    markdown = markdown.lstrip()
    if markdown.startswith("```"):
        _, separator, remainder = markdown.partition("\n")
        if separator:
            markdown = remainder
        else:
            markdown = ""
    markdown = markdown.strip()
    if markdown:
        markdown += "\n"
    return markdown or None


def _decode_json_object(candidate: str) -> dict[str, Any] | None:
    try:
        data = json.loads(candidate.strip())
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None


def _decode_first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
