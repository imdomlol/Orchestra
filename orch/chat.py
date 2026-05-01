"""Terminal chat orchestration driven by Anthropic tool use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Callable, Iterable
from uuid import uuid4

from orch.config import load_config


DEFAULT_MODEL = "claude-opus-4-7"
MAX_RESULT_CHARS = 2048


@dataclass(frozen=True)
class ToolExecution:
    name: str
    arguments: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
        }


class ChatOrchestrator:
    def __init__(
        self,
        root: Path = Path("."),
        *,
        model: str | None = None,
        use_cache: bool = True,
        dry_run: bool = False,
        client: Any | None = None,
        output: Any | None = None,
        input_func: Callable[[str], str] = input,
    ) -> None:
        self.root = root.resolve()
        self.config = load_config(self.root / ".orch" / "config")
        self.model = model or self.config.chat.model or DEFAULT_MODEL
        self.use_cache = use_cache
        self.dry_run = dry_run
        self.output = output if output is not None else sys.stdout
        self.input_func = input_func
        self.messages: list[dict[str, Any]] = []
        self.session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.log_path = self.root / ".orch" / "logs" / "chat" / f"{self.session_id}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client if client is not None else (None if dry_run else self._make_client())

    def run(self, initial_request: str | None = None, *, once: bool = False) -> int:
        if self.dry_run:
            self._write_log(role="system", content="dry-run", tool_calls=[], tool_results=[])
            print(f"dry-run: would start orch chat with {self.model}", file=self.output)
            return 0

        if initial_request:
            self._add_user_text(initial_request)
            finished = self._drive_model_until_waiting()
            if once:
                return 0
            if finished:
                return 0

        while not once:
            try:
                text = self.input_func("you> ")
            except KeyboardInterrupt:
                if self._confirm_exit():
                    return 0
                continue
            except EOFError:
                return 0

            command = text.strip()
            if not command:
                continue
            if command == "/quit":
                return 0
            if command == "/save":
                print(str(self.log_path), file=self.output)
                continue
            if command.startswith("/model "):
                self.model = command.removeprefix("/model ").strip()
                print(f"model: {self.model}", file=self.output)
                continue

            self._add_user_text(text)
            if self._drive_model_until_waiting():
                return 0

        if not initial_request:
            stdin_text = sys.stdin.read().strip()
            if stdin_text:
                self._add_user_text(stdin_text)
                self._drive_model_until_waiting()
        return 0

    def request_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": 4096,
            "system": self._system_blocks(),
            "tools": self.tool_definitions(),
            "messages": self.messages,
        }

    def tool_definitions(self) -> list[dict[str, Any]]:
        tools = [
            {
                "name": "plan",
                "description": "Produce a Markdown plan artifact for a substantial or unfamiliar request.",
                "input_schema": _object_schema({"request": {"type": "string"}}, ["request"]),
            },
            {
                "name": "decompose",
                "description": "Validate task YAML from stdin and write it to .orch/tasks/pending.",
                "input_schema": _object_schema({"yaml_text": {"type": "string"}}, ["yaml_text"]),
            },
            {
                "name": "dispatch",
                "description": "Synchronously dispatch one pending task and run its worker.",
                "input_schema": _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
            },
            {
                "name": "diff",
                "description": "Export and return the main..task/<id> diff for review.",
                "input_schema": _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
            },
            {
                "name": "rework",
                "description": "Append review notes and rerun the worker for a task.",
                "input_schema": _object_schema(
                    {"task_id": {"type": "string"}, "notes": {"type": "string"}},
                    ["task_id", "notes"],
                ),
            },
            {
                "name": "merge",
                "description": "Run integration review and merge one approved task.",
                "input_schema": _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
            },
            {
                "name": "list_tasks",
                "description": "List task YAML paths by state.",
                "input_schema": _object_schema(
                    {
                        "status": {
                            "type": "string",
                            "enum": ["pending", "active", "done", "all"],
                            "default": "all",
                        }
                    },
                    [],
                ),
            },
            {
                "name": "read_file",
                "description": "Read a repo-relative file without modifying it.",
                "input_schema": _object_schema({"path": {"type": "string"}}, ["path"]),
            },
            {
                "name": "run_shell",
                "description": "Run an allowlisted read-only shell command.",
                "input_schema": _object_schema({"command": {"type": "string"}}, ["command"]),
            },
        ]
        if self.use_cache:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tools

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        try:
            if name == "plan":
                return self._run_cli(name, ["plan", _str_arg(arguments, "request")])
            if name == "decompose":
                return self._run_cli(name, ["decompose"], stdin=_str_arg(arguments, "yaml_text"))
            if name == "dispatch":
                return self._run_cli(name, ["dispatch", _str_arg(arguments, "task_id")])
            if name == "diff":
                return self._run_cli(name, ["diff", _str_arg(arguments, "task_id")])
            if name == "rework":
                return self._run_cli(
                    name,
                    ["rework", _str_arg(arguments, "task_id"), "--notes", _str_arg(arguments, "notes")],
                )
            if name == "merge":
                return self._run_cli(name, ["merge", _str_arg(arguments, "task_id")])
            if name == "list_tasks":
                return self._list_tasks(arguments)
            if name == "read_file":
                return self._read_file(arguments)
            if name == "run_shell":
                return self._run_shell(arguments)
        except Exception as exc:
            return ToolExecution(name, arguments, "", str(exc), 1)
        return ToolExecution(name, arguments, "", f"unknown tool: {name}", 1)

    def _drive_model_until_waiting(self) -> bool:
        while True:
            content = self._assistant_turn()
            tool_uses = [block for block in content if _block_type(block) == "tool_use"]
            if not tool_uses:
                return _has_final_summary(content)

            result_blocks = []
            logged_results = []
            for block in tool_uses:
                name = str(_block_value(block, "name", ""))
                arguments = _block_value(block, "input", {}) or {}
                tool_id = str(_block_value(block, "id", ""))
                if not isinstance(arguments, dict):
                    arguments = {}
                print(f"\n-> {name}({json.dumps(arguments, sort_keys=True)})", file=self.output, flush=True)
                result = self.execute_tool(name, arguments)
                print(f"<- {self._summarize_result(result)}", file=self.output, flush=True)
                payload = json.dumps(self._tool_payload_for_model(result), ensure_ascii=False)
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": payload,
                        "is_error": result.exit_code != 0,
                    }
                )
                logged_results.append({"tool_use_id": tool_id, "name": name, **result.as_payload()})
            self.messages.append({"role": "user", "content": result_blocks})
            self._write_log(role="user", content=result_blocks, tool_calls=[], tool_results=logged_results)

    def _assistant_turn(self) -> list[Any]:
        payload = self.request_payload()
        content: list[Any]
        try:
            with self.client.messages.stream(**payload) as stream:
                for text in stream.text_stream:
                    print(text, end="", file=self.output, flush=True)
                message = stream.get_final_message()
            print("", file=self.output, flush=True)
            content = list(getattr(message, "content", []))
        except AttributeError:
            message = self.client.messages.create(**payload)
            content = list(getattr(message, "content", message.get("content", [])))
            for block in content:
                if _block_type(block) == "text":
                    print(str(_block_value(block, "text", "")), end="", file=self.output, flush=True)
            print("", file=self.output, flush=True)

        self.messages.append({"role": "assistant", "content": [_content_block_to_dict(b) for b in content]})
        tool_calls = [_content_block_to_dict(b) for b in content if _block_type(b) == "tool_use"]
        self._write_log(role="assistant", content=[_content_block_to_dict(b) for b in content], tool_calls=tool_calls, tool_results=[])
        return content

    def _add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._write_log(role="user", content=text, tool_calls=[], tool_results=[])

    def _make_client(self) -> Any:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for orch chat; use --dry-run to inspect setup")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("install anthropic>=0.40 to use orch chat") from exc
        return anthropic.Anthropic(api_key=api_key)

    def _system_blocks(self) -> list[dict[str, Any]]:
        blocks = [{"type": "text", "text": SYSTEM_PROMPT}]
        if self.use_cache:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    def _run_cli(self, tool_name: str, args: list[str], *, stdin: str | None = None) -> ToolExecution:
        argv = [sys.executable, "-m", "orch.cli", "--root", str(self.root), *args]
        completed = subprocess.run(
            argv,
            input=stdin,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return ToolExecution(tool_name, {"argv": args}, completed.stdout, completed.stderr, completed.returncode)

    def _list_tasks(self, arguments: dict[str, Any]) -> ToolExecution:
        status = str(arguments.get("status", "all"))
        if status not in {"pending", "active", "done", "all"}:
            return ToolExecution("list_tasks", arguments, "", "status must be pending, active, done, or all", 1)
        return self._run_cli("list_tasks", ["list-tasks", "--status", status])

    def _read_file(self, arguments: dict[str, Any]) -> ToolExecution:
        rel = _str_arg(arguments, "path")
        path = _resolve_repo_path(self.root, rel)
        if not path.is_file():
            return ToolExecution("read_file", arguments, "", f"not a file: {rel}", 1)
        return ToolExecution("read_file", arguments, path.read_text(encoding="utf-8"), "", 0)

    def _run_shell(self, arguments: dict[str, Any]) -> ToolExecution:
        command = _str_arg(arguments, "command")
        argv = shlex.split(command)
        if not _is_allowed_shell(argv):
            return ToolExecution("run_shell", arguments, "", f"command is not allowlisted: {command}", 1)
        completed = subprocess.run(
            argv,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return ToolExecution("run_shell", arguments, completed.stdout, completed.stderr, completed.returncode)

    def _summarize_result(self, result: ToolExecution) -> str:
        payload = result.stdout.strip() or result.stderr.strip() or f"exit {result.exit_code}"
        if len(payload) > MAX_RESULT_CHARS:
            payload = payload[:MAX_RESULT_CHARS] + f"... [truncated; full result in {self.log_path}]"
        return payload.replace("\n", "\\n")

    def _tool_payload_for_model(self, result: ToolExecution) -> dict[str, Any]:
        payload = result.as_payload()
        for key in ("stdout", "stderr"):
            value = str(payload[key])
            if len(value) > MAX_RESULT_CHARS:
                payload[key] = value[:MAX_RESULT_CHARS] + f"... [truncated; full result in {self.log_path}]"
        return payload

    def _write_log(
        self,
        *,
        role: str,
        content: Any,
        tool_calls: list[Any],
        tool_results: list[Any],
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _confirm_exit(self) -> bool:
        try:
            answer = self.input_func("\nExit orch chat? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return True
        return answer in {"y", "yes"}


SYSTEM_PROMPT = """You are the Claude Opus terminal orchestrator for this repo.

You never edit source directly. You coordinate work through the provided tools.
Confirm intent and ask clarifying questions when a request is ambiguous.
Use plan for big refactors, unfamiliar areas, or high-risk changes; for small
well-scoped changes, author task YAML directly. Task YAML must match
.orch/schemas/task.schema.json and must be sent through decompose before work.
Run dispatch for each task. Parallelize dispatches only when owned_files are
disjoint and dependencies allow it. After each worker finishes, call diff, read
the patch, and choose approve, rework, or abandon. Use rework with concrete
notes when changes are needed. Use merge only after you approve the diff.
When all accepted tasks are merged, summarize what changed and any verification
that ran. Keep tool results small and inspect files with read_file or
allowlisted run_shell commands when needed."""


def run_chat(
    *,
    root: Path,
    request: str | None = None,
    model: str | None = None,
    no_cache: bool = False,
    once: bool = False,
    dry_run: bool = False,
) -> int:
    session = ChatOrchestrator(
        root,
        model=model,
        use_cache=not no_cache,
        dry_run=dry_run,
    )
    return session.run(request, once=once)


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _str_arg(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _resolve_repo_path(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes repo: {rel}") from exc
    return path


def _is_allowed_shell(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[:2] == ["git", "status"]:
        return True
    if argv[:2] == ["git", "log"]:
        return True
    if argv[0] == "ls":
        return True
    if argv[0] == "rg":
        return True
    if argv[0] in {"cat", "sed", "head", "tail"}:
        if argv[0] == "sed" and any(arg == "-i" or arg.startswith("-i.") for arg in argv[1:]):
            return False
        return True
    return False


def _block_type(block: Any) -> str:
    return str(_block_value(block, "type", ""))


def _block_value(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    data = {"type": _block_type(block)}
    for key in ("text", "id", "name", "input"):
        value = getattr(block, key, None)
        if value is not None:
            data[key] = value
    return data


def _has_final_summary(content: Iterable[Any]) -> bool:
    for block in content:
        if _block_type(block) != "text":
            continue
        if "final summary" in str(_block_value(block, "text", "")).lower():
            return True
    return False
