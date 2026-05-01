"""Terminal chat orchestration driven by Claude Agent SDK tool use."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterable
from uuid import uuid4

from orch.config import load_config


DEFAULT_MODEL = "claude-opus-4-7"
MAX_RESULT_CHARS = 2048
DISPLAY_PREVIEW_CHARS = 900
DISPLAY_PREVIEW_LINES = 10
CREDENTIAL_ERROR = (
    "Claude credentials are required for orch chat; run `claude login` for "
    "Claude Code OAuth or set ANTHROPIC_API_KEY. Use --dry-run to inspect setup."
)


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
        query_func: Any | None = None,
        sdk: Any | None = None,
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
        self.client = client
        self.query_func = query_func
        self.sdk = sdk
        self.messages: list[dict[str, Any]] = []
        self._sdk_tool_counter = 0
        self.session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.log_path = self.root / ".orch" / "logs" / "chat" / f"{self.session_id}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def run(self, initial_request: str | None = None, *, once: bool = False) -> int:
        if self.dry_run:
            self._write_log(role="system", content="dry-run", tool_calls=[], tool_results=[])
            print(f"dry-run: would start orch chat with {self.model}", file=self.output)
            return 0

        if once:
            request = initial_request or sys.stdin.read().strip()
            if request:
                self._add_user_text(request)
                try:
                    return asyncio.run(self._run_once(request))
                except KeyboardInterrupt:
                    return 0 if self._confirm_exit() else 130
            return 0

        try:
            return asyncio.run(self._run_interactive(initial_request))
        except KeyboardInterrupt:
            return 0 if self._confirm_exit() else 130

    def request_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
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

    async def _run_once(self, request: str) -> int:
        query_func = self.query_func or self._load_sdk().query
        try:
            await self._consume_response(query_func(prompt=request, options=self._sdk_options()))
        except Exception as exc:
            raise RuntimeError(_credential_error_message(exc)) from exc
        return 0

    async def _run_interactive(self, initial_request: str | None) -> int:
        client = self.client or self._load_sdk().ClaudeSDKClient(options=self._sdk_options())
        try:
            async with client:
                if initial_request:
                    self._add_user_text(initial_request)
                    await client.query(initial_request)
                    if await self._consume_response(client.receive_response()):
                        return 0

                while True:
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
                        if hasattr(client, "set_model"):
                            await client.set_model(self.model)
                        print(f"model: {self.model}", file=self.output)
                        continue

                    self._add_user_text(text)
                    await client.query(text)
                    if await self._consume_response(client.receive_response()):
                        return 0
        except Exception as exc:
            raise RuntimeError(_credential_error_message(exc)) from exc
        return 0

    async def _consume_response(self, messages: Any) -> bool:
        content: list[Any] = []
        streamed_text = False
        async for message in messages:
            if _message_kind(message) == "stream_event":
                streamed_text = self._render_stream_event(message) or streamed_text
                continue
            if _message_kind(message) == "assistant":
                blocks = list(getattr(message, "content", []))
                content.extend(blocks)
                if not streamed_text:
                    for block in blocks:
                        if _block_type(block) == "text":
                            print(str(_block_value(block, "text", "")), end="", file=self.output, flush=True)
                block_dicts = [_content_block_to_dict(b) for b in blocks]
                self.messages.append({"role": "assistant", "content": block_dicts})
                self._write_log(
                    role="assistant",
                    content=block_dicts,
                    tool_calls=[b for b in block_dicts if b.get("type") == "tool_use"],
                    tool_results=[],
                )
                continue
            if _message_kind(message) == "result":
                print("", file=self.output, flush=True)
                if getattr(message, "is_error", False):
                    errors = getattr(message, "errors", None) or [getattr(message, "result", "Claude returned an error")]
                    raise RuntimeError("; ".join(str(error) for error in errors if error))
        return _has_final_summary(content)

    def _render_stream_event(self, message: Any) -> bool:
        event = getattr(message, "event", {})
        if not isinstance(event, dict) or event.get("type") != "content_block_delta":
            return False
        delta = event.get("delta", {})
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return False
        text = delta.get("text")
        if not isinstance(text, str):
            return False
        print(text, end="", file=self.output, flush=True)
        return True

    def _sdk_options(self) -> Any:
        sdk = self._load_sdk()
        definitions = self.tool_definitions()
        server = sdk.create_sdk_mcp_server(
            name="orchestra",
            version="1.0.0",
            tools=[self._sdk_tool_for(definition) for definition in definitions],
        )
        return sdk.ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={"orchestra": server},
            allowed_tools=[f"mcp__orchestra__{definition['name']}" for definition in definitions],
            tools=[],
            model=self.model,
            cwd=self.root,
        )

    def _sdk_tool_for(self, definition: dict[str, Any]) -> Any:
        sdk = self._load_sdk()
        name = str(definition["name"])
        description = str(definition["description"])
        input_schema = _schema_without_cache(definition["input_schema"])

        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return self._handle_sdk_tool_call(name, arguments)

        return sdk.tool(name, description, input_schema)(handler)

    def _handle_sdk_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            arguments = {}
        self._sdk_tool_counter += 1
        tool_id = f"sdk-tool-{self._sdk_tool_counter}"
        print(f"\n→ {name}({_format_tool_args(arguments)})", file=self.output, flush=True)
        result = self.execute_tool(name, arguments)
        print(f"← {self._summarize_result(result)}", file=self.output, flush=True)
        payload = self._tool_payload_for_model(result)
        result_block = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": json.dumps(payload, ensure_ascii=False),
            "is_error": result.exit_code != 0,
        }
        self.messages.append({"role": "user", "content": [result_block]})
        self._write_log(
            role="user",
            content=[result_block],
            tool_calls=[],
            tool_results=[{"tool_use_id": tool_id, "name": name, **result.as_payload()}],
        )
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "is_error": result.exit_code != 0,
        }

    def _load_sdk(self) -> Any:
        if self.sdk is not None:
            return self.sdk
        try:
            import claude_agent_sdk
        except ImportError as exc:
            if not self._has_obvious_credentials():
                raise RuntimeError(CREDENTIAL_ERROR) from exc
            raise RuntimeError("install claude-agent-sdk>=0.1 to use orch chat") from exc
        self.sdk = claude_agent_sdk
        return self.sdk

    def _has_obvious_credentials(self) -> bool:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return True
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        if (config_dir / ".credentials.json").exists():
            return True
        return False

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
        if shutil.which(argv[0]) is None:
            extra = " Use grep for repository text search." if argv[0] == "rg" else ""
            return ToolExecution("run_shell", arguments, "", f"command not found: {argv[0]}.{extra}", 127)
        completed = subprocess.run(
            argv,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return ToolExecution("run_shell", arguments, completed.stdout, completed.stderr, completed.returncode)

    def _summarize_result(self, result: ToolExecution) -> str:
        payload = result.stdout.strip() or result.stderr.strip()
        if not payload:
            return f"exit {result.exit_code}"

        text = _display_preview(payload)
        status = "ok" if result.exit_code == 0 else f"exit {result.exit_code}"
        if result.name == "read_file":
            path = result.arguments.get("path", "file")
            return _summary_with_preview(f"{status}; read {path}", payload, text, self.log_path)
        if result.name == "run_shell":
            command = result.arguments.get("command", "command")
            return _summary_with_preview(f"{status}; `{command}`", payload, text, self.log_path)
        return _summary_with_preview(status, payload, text, self.log_path)

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

    def _add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._write_log(role="user", content=text, tool_calls=[], tool_results=[])

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


def _schema_without_cache(schema: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(schema)
    cleaned.pop("cache_control", None)
    return cleaned


def _format_tool_args(arguments: dict[str, Any]) -> str:
    parts = []
    for key, value in arguments.items():
        if isinstance(value, str):
            display = value.replace("\n", "\\n")
            if len(display) > 80:
                display = display[:77] + "..."
            parts.append(f"{key}={display!r}")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def _display_preview(payload: str) -> str:
    lines = payload.splitlines()
    preview = "\n".join(lines[:DISPLAY_PREVIEW_LINES])
    if len(preview) > DISPLAY_PREVIEW_CHARS:
        preview = preview[:DISPLAY_PREVIEW_CHARS].rstrip()
    return preview


def _summary_with_preview(header: str, payload: str, preview: str, log_path: Path) -> str:
    lines = payload.splitlines()
    truncated = len(lines) > DISPLAY_PREVIEW_LINES or len(preview) < len(payload)
    if not preview:
        return header
    suffix = f"\n  ... full result in {log_path}" if truncated else ""
    indented = "\n  ".join(preview.splitlines())
    return f"{header}\n  {indented}{suffix}"


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
    if argv[0] == "grep":
        return True
    if argv[0] in {"cat", "sed", "head", "tail"}:
        if argv[0] == "sed" and any(arg == "-i" or arg.startswith("-i.") for arg in argv[1:]):
            return False
        return True
    return False


def _message_kind(message: Any) -> str:
    name = message.__class__.__name__
    if name == "AssistantMessage":
        return "assistant"
    if name == "ResultMessage":
        return "result"
    if name == "StreamEvent":
        return "stream_event"
    if isinstance(message, dict):
        return str(message.get("type", ""))
    return ""


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type", ""))
    name = block.__class__.__name__
    if name == "TextBlock":
        return "text"
    if name == "ToolUseBlock":
        return "tool_use"
    if name == "ToolResultBlock":
        return "tool_result"
    if name == "ThinkingBlock":
        return "thinking"
    if name == "ServerToolUseBlock":
        return "server_tool_use"
    if name == "ServerToolResultBlock":
        return "server_tool_result"
    return str(getattr(block, "type", ""))


def _block_value(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    data = {"type": _block_type(block)}
    if hasattr(block, "model_dump"):
        dumped = block.model_dump()
        dumped.setdefault("type", data["type"])
        return dumped
    for key in ("text", "thinking", "signature", "id", "name", "input", "tool_use_id", "content", "is_error"):
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


def _credential_error_message(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if (
        "authentication" in lowered
        or "credentials" in lowered
        or "api key" in lowered
        or "oauth" in lowered
        or "login" in lowered
    ):
        return CREDENTIAL_ERROR
    return text
