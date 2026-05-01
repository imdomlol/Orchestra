from __future__ import annotations

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


class FakeStream:
    def __init__(self, message: SimpleNamespace) -> None:
        self.message = message
        self.text_stream = [
            block["text"] for block in message.content if block.get("type") == "text"
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get_final_message(self):
        return self.message


class FakeMessages:
    def __init__(self, responses: list[list[dict]]) -> None:
        self.responses = responses
        self.payloads: list[dict] = []

    def stream(self, **payload):
        self.payloads.append(payload)
        return FakeStream(SimpleNamespace(content=self.responses.pop(0)))


class FakeClient:
    def __init__(self, responses: list[list[dict]]) -> None:
        self.messages = FakeMessages(responses)


def tool_use(name: str, arguments: dict, suffix: str = "") -> dict:
    return {
        "type": "tool_use",
        "id": f"tool-{name}{suffix}",
        "name": name,
        "input": arguments,
    }


def test_tool_definitions_match_documented_schema(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    chat = ChatOrchestrator(repo, client=FakeClient([]))

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
    client = FakeClient(
        [
            [tool_use("plan", {"request": "ship it"})],
            [tool_use("decompose", {"yaml_text": "id: T-0001\n"})],
            [tool_use("dispatch", {"task_id": "T-0001"})],
            [tool_use("diff", {"task_id": "T-0001"})],
            [tool_use("merge", {"task_id": "T-0001"})],
            [{"type": "text", "text": "Final summary: merged T-0001."}],
        ]
    )
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(argv, *, input=None, cwd=None, text=None, capture_output=None, check=None):
        calls.append((list(argv), input))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    code = ChatOrchestrator(repo, client=client).run("ship it", once=True)

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
    result = ChatOrchestrator(repo, client=FakeClient([])).execute_tool(
        "dispatch", {"task_id": "T-0001"}
    )

    assert result.exit_code == 2
    assert result.stderr == "bad task"


def test_run_shell_rejects_non_allowlisted_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    result = ChatOrchestrator(repo, client=FakeClient([])).execute_tool(
        "run_shell", {"command": "python -c 'print(1)'"}
    )

    assert result.exit_code == 1
    assert "not allowlisted" in result.stderr


def test_session_transcript_is_written(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    client = FakeClient([[{"type": "text", "text": "hello"}]])
    chat = ChatOrchestrator(repo, client=client)

    chat.run("say hi", once=True)

    assert chat.log_path.exists()
    assert chat.log_path.read_text(encoding="utf-8").count("\n") >= 2


def test_prompt_caching_marks_system_and_last_tool(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    copy_chat_layout(repo)
    chat = ChatOrchestrator(repo, client=FakeClient([]))

    payload = chat.request_payload()

    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
