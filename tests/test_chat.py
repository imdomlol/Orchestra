from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from orch.chat import ChatOrchestrator


def copy_chat_layout(repo: Path) -> None:
    (repo / ".orch").mkdir()
    shutil.copytree(".orch/config", repo / ".orch/config")
    shutil.copytree(".orch/schemas", repo / ".orch/schemas")
    for directory in [
        ".orch/tasks/pending",
        ".orch/tasks/active",
        ".orch/tasks/done",
        ".orch/logs/chat",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-opus-4-7"


@dataclass
class ResultMessage:
    is_error: bool = False
    errors: list[str] | None = None
    result: str | None = None


@dataclass
class ClaudeAgentOptions:
    system_prompt: str
    mcp_servers: dict
    allowed_tools: list[str]
    tools: list[str]
    model: str
    cwd: Path


class FakeSDK:
    ClaudeAgentOptions = ClaudeAgentOptions

    @staticmethod
    def tool(name, description, input_schema):
        def decorator(handler):
            return SimpleNamespace(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
            )

        return decorator

    @staticmethod
    def create_sdk_mcp_server(name, version, tools):
        return {"type": "sdk", "name": name, "version": version, "tools": tools}


def tool_by_name(options: ClaudeAgentOptions, name: str):
    for tool in options.mcp_servers["orchestra"]["tools"]:
        if tool.name == name:
            return tool
    raise AssertionError(f"missing tool: {name}")


def scripted_query(steps: list[tuple[str, dict]]):
    async def query(*, prompt: str, options: ClaudeAgentOptions):
        for index, (name, arguments) in enumerate(steps, start=1):
            yield AssistantMessage([ToolUseBlock(f"tool-{index}", name, arguments)])
            await tool_by_name(options, name).handler(arguments)
        yield AssistantMessage([TextBlock("Final summary: merged T-0001.")])
        yield ResultMessage()

    return query


def test_tool_definitions_match_documented_schema(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    (repo / "orch").mkdir()
    shutil.copy("orch/config.py", repo / "orch/config.py")
    chat = ChatOrchestrator(repo, sdk=FakeSDK)

    tools = {tool["name"]: tool for tool in chat.tool_definitions()}

    assert list(tools) == [
        "plan",
        "decompose",
        "dispatch",
        "diff",
        "rework",
        "merge",
        "list_tasks",
        "read_file",
        "run_shell",
    ]
    assert tools["plan"]["input_schema"]["required"] == ["request"]
    assert tools["list_tasks"]["input_schema"]["properties"]["status"]["enum"] == [
        "pending",
        "active",
        "done",
        "all",
    ]


def test_scripted_conversation_drives_cli_subprocesses_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(argv, *, input=None, cwd=None, text=None, capture_output=None, check=None):
        calls.append((list(argv), input))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    query = scripted_query(
        [
            ("plan", {"request": "ship it"}),
            ("decompose", {"yaml_text": "id: T-0001\n"}),
            ("dispatch", {"task_id": "T-0001"}),
            ("diff", {"task_id": "T-0001"}),
            ("merge", {"task_id": "T-0001"}),
        ]
    )

    code = ChatOrchestrator(repo, sdk=FakeSDK, query_func=query).run("ship it", once=True)

    assert code == 0
    invoked = []
    for argv, _stdin in calls:
        root_index = argv.index("--root")
        invoked.append(argv[root_index + 2 :])

    assert invoked == [
        ["plan", "ship it"],
        ["decompose"],
        ["dispatch", "T-0001"],
        ["diff", "T-0001"],
        ["merge", "T-0001"],
    ]
    assert calls[1][1] == "id: T-0001\n"


def test_tool_errors_are_returned_to_model_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad task")

    monkeypatch.setattr("subprocess.run", fake_run)
    result = ChatOrchestrator(repo, sdk=FakeSDK).execute_tool(
        "dispatch", {"task_id": "T-0001"}
    )

    assert result.exit_code == 2
    assert result.stderr == "bad task"


def test_sdk_tool_errors_are_returned_to_model_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad task")

    monkeypatch.setattr("subprocess.run", fake_run)
    response = ChatOrchestrator(repo, sdk=FakeSDK)._handle_sdk_tool_call(
        "dispatch", {"task_id": "T-0001"}
    )

    assert response["is_error"] is True
    assert "bad task" in response["content"][0]["text"]


def test_tool_display_summarizes_multiline_file_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    (repo / "orch").mkdir()
    shutil.copy("orch/config.py", repo / "orch/config.py")
    chat = ChatOrchestrator(repo, sdk=FakeSDK)
    result = chat.execute_tool("read_file", {"path": "orch/config.py"})

    summary = chat._summarize_result(result)

    assert "ok; read orch/config.py" in summary
    assert "\\n" not in summary
    assert "full result in" in summary


def test_run_shell_rejects_non_allowlisted_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    result = ChatOrchestrator(repo, sdk=FakeSDK).execute_tool(
        "run_shell", {"command": "python -c 'print(1)'"}
    )

    assert result.exit_code == 1
    assert "not allowlisted" in result.stderr


def test_run_shell_allows_grep_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="match\n", stderr="")

    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("subprocess.run", fake_run)

    result = ChatOrchestrator(repo, sdk=FakeSDK).execute_tool(
        "run_shell", {"command": "grep -rn load_config orch tests"}
    )

    assert result.exit_code == 0
    assert result.stdout == "match\n"
    assert calls == [["grep", "-rn", "load_config", "orch", "tests"]]


def test_run_shell_reports_missing_rg_with_grep_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    monkeypatch.setattr("shutil.which", lambda command: None)

    result = ChatOrchestrator(repo, sdk=FakeSDK).execute_tool(
        "run_shell", {"command": "rg -n load_config orch"}
    )

    assert result.exit_code == 127
    assert "command not found: rg" in result.stderr
    assert "grep" in result.stderr


def test_keyboard_interrupt_exits_cleanly_when_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    chat = ChatOrchestrator(repo, sdk=FakeSDK, input_func=lambda prompt: "y")

    async def interrupted(initial_request):
        raise KeyboardInterrupt

    monkeypatch.setattr(chat, "_run_interactive", interrupted)

    assert chat.run("ship it") == 0


def test_session_transcript_is_written(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    query = scripted_query([])
    chat = ChatOrchestrator(repo, sdk=FakeSDK, query_func=query)

    chat.run("say hi", once=True)

    assert chat.log_path.exists()
    assert chat.log_path.read_text(encoding="utf-8").count("\n") >= 2


def test_sdk_options_expose_orchestra_tools_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    chat = ChatOrchestrator(repo, sdk=FakeSDK)

    options = chat._sdk_options()

    assert options.system_prompt == chat.request_payload()["system"][0]["text"]
    assert options.tools == []
    assert "mcp__orchestra__plan" in options.allowed_tools
    assert [tool.name for tool in options.mcp_servers["orchestra"]["tools"]] == [
        "plan",
        "decompose",
        "dispatch",
        "diff",
        "rework",
        "merge",
        "list_tasks",
        "read_file",
        "run_shell",
    ]
