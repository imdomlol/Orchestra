from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys

from orch.inbox import Inbox
from orch.model_wrapper import ModelWrapper, extract_handoff
from orch.wrapper_cli import planner_main


def copy_config(repo: Path, *, gemini: str | None = None, codex: str | None = None) -> None:
    shutil.copytree(".orch/config", repo / ".orch/config")
    config_path = repo / ".orch/config/orchestrator.toml"
    text = config_path.read_text(encoding="utf-8")
    if gemini is not None:
        text = text.replace('gemini = "gemini"', f"gemini = {json.dumps(gemini)}")
    if codex is not None:
        text = text.replace('codex = "codex"', f"codex = {json.dumps(codex)}")
    config_path.write_text(text, encoding="utf-8")


def fake_cli_command(action: str) -> str:
    script = (
        "import json, sys; "
        "prompt = sys.stdin.read(); "
        f"print('ORCH_HANDOFF:' + json.dumps({{'action': {action!r}, "
        "'prompt_has_context': 'Invocation Context' in prompt}))"
    )
    return f"{sys.executable} -c {json.dumps(script)}"


def test_wrapper_injects_prompt_runs_cli_and_posts_handoff(tmp_path: Path) -> None:
    copy_config(tmp_path, gemini=fake_cli_command("planned"))
    request_path = tmp_path / ".orch/requests/R-0001.md"
    request_path.parent.mkdir(parents=True)
    request_path.write_text("Build something.\n", encoding="utf-8")

    result = ModelWrapper(tmp_path).run_role(
        "gemini-planner",
        request_path=".orch/requests/R-0001.md",
        log_name="P-0001",
        timeout_seconds=5,
    )

    assert result.succeeded
    assert result.handoff == {
        "action": "planned",
        "prompt_has_context": True,
        "role": "gemini-planner",
    }
    assert result.process.stdout_path == tmp_path / ".orch/logs/planner/P-0001.stdout"
    assert Inbox(tmp_path).read_next("orchestrator").body["action"] == "planned"


def test_worker_wrapper_runs_from_worktree(tmp_path: Path) -> None:
    copy_config(tmp_path, codex=fake_cli_command("worker_done"))
    worktree = tmp_path / ".orch/worktrees/T-0001"
    worktree.mkdir(parents=True)

    result = ModelWrapper(tmp_path).run_role(
        "codex-worker",
        task_id="T-0001",
        task_yaml_path=".orch/tasks/active/T-0001.yaml",
        worktree_path=".orch/worktrees/T-0001",
        timeout_seconds=5,
    )

    assert result.succeeded
    assert result.process.cwd == worktree
    assert result.handoff_path == Inbox(tmp_path).read_next("orchestrator").path


def test_extract_handoff_accepts_plain_json_or_prefixed_line() -> None:
    assert extract_handoff('{"action": "ok"}\n') == {"action": "ok"}
    assert extract_handoff('noise\nORCH_HANDOFF: {"action": "ok"}\n') == {"action": "ok"}
    assert extract_handoff("noise only\n") is None


def test_planner_console_entrypoint_reports_paths(tmp_path: Path, capsys) -> None:
    copy_config(tmp_path, gemini=fake_cli_command("planned"))

    code = planner_main(
        [
            "--root",
            str(tmp_path),
            "--request-path",
            ".orch/requests/R-0001.md",
            "--log-name",
            "P-0001",
            "--timeout-seconds",
            "5",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["handoff_path"].endswith(".json")
    assert output["stdout_path"].endswith(".orch/logs/planner/P-0001.stdout")
