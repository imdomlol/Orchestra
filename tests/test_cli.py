from __future__ import annotations

import io
from pathlib import Path
import shutil
import subprocess

import yaml

import orch.cli as cli
from orch.cli import main
from orch.doctor import DoctorCheck, DoctorReport
from orch.inbox import Inbox
from orch.runtime import RunLoopResult, RunOnceResult
from orch.task_store import TaskStore


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.local")
    git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def copy_configured_layout(repo: Path) -> None:
    for directory in [
        ".orch/tasks/pending",
        ".orch/tasks/active",
        ".orch/tasks/done",
        ".orch/locks",
        ".orch/schemas",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copytree(".orch/config", repo / ".orch/config")
    shutil.copy(".orch/schemas/task.schema.json", repo / ".orch/schemas/task.schema.json")


def load_task(task_id: str = "T-0001") -> dict:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text(encoding="utf-8"))
    task["id"] = task_id
    task["branch"] = f"task/{task_id}"
    task["worktree_path"] = f".orch/worktrees/{task_id}"
    return task


def test_submit_cli_records_request(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    code = main(["--root", str(repo), "submit", "Ship it"])

    assert code == 0
    output = capsys.readouterr().out.strip()
    assert output.startswith(str(repo / ".orch/requests/R-"))
    assert Inbox(repo).read_next("orchestrator") is not None


def test_plan_cli_prints_plan_path(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    plan_path = repo / ".orch/plans/P-0001.md"
    calls: list[dict] = []

    class FakeRuntime:
        @classmethod
        def from_config(cls, *, root: Path):
            assert root == repo
            return cls()

        def plan_only(self, request: str):
            calls.append({"request": request})
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("# Plan\n", encoding="utf-8")
            return plan_path

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "plan", "add foo"])

    assert code == 0
    assert capsys.readouterr().out.strip() == str(plan_path)
    assert calls == [{"request": "add foo"}]


def test_decompose_cli_writes_stdin_task_yaml_and_prints_path(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    task = load_task("T-0025")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(yaml.safe_dump(task, sort_keys=False)),
    )

    code = main(["--root", str(repo), "decompose"])

    assert code == 0
    path = repo / ".orch/tasks/pending/T-0025.yaml"
    assert capsys.readouterr().out.strip() == str(path)
    assert TaskStore(repo).read("T-0025")["status"] == "pending"


def test_decompose_cli_rejects_schema_invalid_yaml_without_write(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    task = load_task("T-0025")
    task["acceptance_criteria"] = []
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(yaml.safe_dump(task, sort_keys=False)),
    )

    code = main(["--root", str(repo), "decompose"])

    captured = capsys.readouterr()
    assert code == 1
    assert "failed validation" in captured.err
    assert TaskStore(repo).list_tasks("pending") == []


def test_decompose_cli_rejects_duplicate_ids_without_overwrite(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    original = load_task("T-0025")
    TaskStore(repo).write_pending(original)
    duplicate = load_task("T-0025")
    duplicate["objective"] = "A different task with the same id."
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(yaml.safe_dump(duplicate, sort_keys=False)),
    )

    code = main(["--root", str(repo), "decompose"])

    captured = capsys.readouterr()
    assert code == 1
    assert "task already exists: T-0025" in captured.err
    assert TaskStore(repo).read("T-0025")["objective"] == original["objective"]


def test_decompose_cli_rejects_malformed_yaml_without_write(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    monkeypatch.setattr("sys.stdin", io.StringIO("id: ["))

    code = main(["--root", str(repo), "decompose"])

    captured = capsys.readouterr()
    assert code == 1
    assert "malformed task YAML" in captured.err
    assert TaskStore(repo).list_tasks("pending") == []


def test_dispatch_cli_runs_named_task_and_prints_active_path(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    active_path = repo / ".orch/tasks/active/T-0001.yaml"
    calls: list[dict] = []

    class DispatchResult:
        task_path = active_path

    class FakeRuntime:
        @classmethod
        def from_config(
            cls,
            *,
            root: Path,
            on_progress=None,
            model_stderr_sink=None,
        ):
            assert root == repo
            return cls()

        def dispatch_task(self, task_id: str):
            calls.append({"task_id": task_id})
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text("id: T-0001\n", encoding="utf-8")
            return DispatchResult()

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "dispatch", "T-0001"])

    assert code == 0
    assert capsys.readouterr().out.strip() == str(active_path)
    assert calls == [{"task_id": "T-0001"}]


def test_dispatch_cli_owned_file_collision_exits_nonzero_and_keeps_pending(
    tmp_path: Path,
    capsys,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    store = TaskStore(repo)
    active = load_task("T-0002")
    active["owned_files"] = ["orch/**"]
    pending = load_task("T-0001")
    pending["owned_files"] = ["orch/cli.py"]
    store.write_pending(active)
    store.transition("T-0002", "active", "in_progress")
    store.write_pending(pending)

    code = main(["--root", str(repo), "dispatch", "T-0001"])

    captured = capsys.readouterr()
    assert code == 1
    assert "owned_files collide" in captured.err
    assert (repo / ".orch/tasks/pending/T-0001.yaml").exists()
    assert not (repo / ".orch/tasks/active/T-0001.yaml").exists()


def test_diff_cli_prints_exported_patch(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    calls: list[str] = []

    class DiffResult:
        contents = "diff --git a/feature.txt b/feature.txt\n+hello\n"

    class FakeRuntime:
        @classmethod
        def from_config(cls, *, root: Path):
            assert root == repo
            return cls()

        def export_diff(self, task_id: str):
            calls.append(task_id)
            return DiffResult()

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "diff", "T-0001"])

    assert code == 0
    assert capsys.readouterr().out == DiffResult.contents
    assert calls == ["T-0001"]


def test_diff_cli_reports_export_failure(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    class FakeRuntime:
        @classmethod
        def from_config(cls, *, root: Path):
            return cls()

        def export_diff(self, task_id: str):
            raise RuntimeError(f"failed to export review diff for {task_id}: bad ref")

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "diff", "T-0001"])

    assert code == 1
    assert "bad ref" in capsys.readouterr().err


def test_rework_cli_runs_task_and_prints_active_path(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    active_path = repo / ".orch/tasks/active/T-0001.yaml"
    calls: list[dict] = []

    class ReworkResult:
        task_path = active_path

    class FakeRuntime:
        @classmethod
        def from_config(
            cls,
            *,
            root: Path,
            on_progress=None,
            model_stderr_sink=None,
        ):
            assert root == repo
            return cls()

        def rework_task(self, task_id: str, notes: str):
            calls.append({"task_id": task_id, "notes": notes})
            return ReworkResult()

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "rework", "T-0001", "--notes", "Fix tests"])

    assert code == 0
    assert capsys.readouterr().out.strip() == str(active_path)
    assert calls == [{"task_id": "T-0001", "notes": "Fix tests"}]


def test_rework_cli_reports_worker_failure(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    class FakeRuntime:
        @classmethod
        def from_config(
            cls,
            *,
            root: Path,
            on_progress=None,
            model_stderr_sink=None,
        ):
            return cls()

        def rework_task(self, task_id: str, notes: str):
            raise RuntimeError("T-0001 dispatch failed: codex-worker exited with 2")

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "rework", "T-0001", "--notes", "Fix tests"])

    assert code == 1
    assert "codex-worker exited with 2" in capsys.readouterr().err


def test_merge_cli_reports_driver_success(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    calls: list[str] = []

    class MergeInner:
        message = "merged"

    class MergeResult:
        merged = True
        merge_result = MergeInner()

    class FakeRuntime:
        @classmethod
        def from_config(
            cls,
            *,
            root: Path,
            on_progress=None,
            model_stderr_sink=None,
        ):
            assert root == repo
            return cls()

        def merge_task(self, task_id: str):
            calls.append(task_id)
            return MergeResult()

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "merge", "T-0001"])

    assert code == 0
    assert capsys.readouterr().out.strip() == "merged"
    assert calls == ["T-0001"]


def test_merge_cli_reports_driver_failure(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    class MergeInner:
        message = "patch conflict"
        status = "conflict"

    class MergeResult:
        merged = False
        merge_result = MergeInner()

    class FakeRuntime:
        @classmethod
        def from_config(
            cls,
            *,
            root: Path,
            on_progress=None,
            model_stderr_sink=None,
        ):
            return cls()

        def merge_task(self, task_id: str):
            return MergeResult()

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "merge", "T-0001"])

    captured = capsys.readouterr()
    assert code == 1
    assert "patch conflict" in captured.err


def test_merge_cli_clean_patch_merges_to_main_and_moves_task_done(
    tmp_path: Path,
    capsys,
) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    store = TaskStore(repo)
    task = load_task()
    task["owned_files"] = ["feature.txt"]
    task["allowed_commands"] = []
    store.write_pending(task)
    store.transition("T-0001", "active", "self_review")
    worktree = repo / ".orch/worktrees/T-0001"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", "task/T-0001", str(worktree), "main")
    (worktree / "feature.txt").write_text("hello\n", encoding="utf-8")
    git(worktree, "add", "feature.txt")
    git(worktree, "commit", "-m", "add feature")

    code = main(["--root", str(repo), "merge", "T-0001"])

    assert code == 0
    assert "merged" in capsys.readouterr().out
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    assert (repo / ".orch/tasks/done/T-0001.yaml").exists()
    log = git(repo, "log", "--oneline", "-1").stdout
    assert "merge(T-0001):" in log


def test_run_once_cli_reports_idle(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    code = main(["--root", str(repo), "run", "--once"])

    assert code == 0
    assert "Idle" in capsys.readouterr().out


def test_run_cli_starts_continuous_loop(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    calls: list[dict] = []

    class FakeRuntime:
        @classmethod
        def from_config(cls, *, root: Path, on_progress=None, on_confirm=None, model_stderr_sink=None):
            assert root == repo
            return cls()

        def run(self, *, stop_requested, on_result):
            calls.append({"stop_requested": stop_requested, "on_result": on_result})
            on_result(RunOnceResult(kind="idle", message="no actionable work"))
            return RunLoopResult(
                kind="stopped",
                message="run loop stopped",
                iterations=1,
                last_result=None,
            )

    monkeypatch.setattr(cli, "OrchestraRuntime", FakeRuntime)

    code = main(["--root", str(repo), "run"])

    assert code == 0
    assert len(calls) == 1
    assert "Idle" in capsys.readouterr().out


def test_chat_cli_dry_run_without_api_key_exits_zero(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))

    code = main(["--root", str(repo), "chat", "--once", "--dry-run", "say hi"])

    assert code == 0
    assert "dry-run" in capsys.readouterr().out


def test_chat_cli_requires_credentials_without_dry_run(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))

    code = main(["--root", str(repo), "chat", "--once", "say hi"])

    assert code == 1
    err = capsys.readouterr().err
    assert "claude login" in err
    assert "ANTHROPIC_API_KEY" in err


def test_image_build_prints_configured_docker_command(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    code = main(["--root", str(repo), "image", "build", "--print", "--pull"])

    assert code == 0
    output = capsys.readouterr().out.strip()
    assert "docker build" in output
    assert "--tag orchestra-sandbox:py3.12" in output
    assert "--pull" in output
    assert str(repo / "docker/orchestra-sandbox.Dockerfile") in output


def test_doctor_cli_prints_checks_and_sets_exit_code(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = init_repo(tmp_path)
    copy_configured_layout(repo)

    class FakeDoctor:
        @classmethod
        def from_config(cls, *, root: Path):
            assert root == repo
            return cls()

        def run(self):
            return DoctorReport(
                (
                    DoctorCheck("gemini CLI", True, "gemini 1.0"),
                    DoctorCheck("docker daemon", False, "daemon unavailable"),
                )
            )

    monkeypatch.setattr(cli, "Doctor", FakeDoctor)

    code = main(["--root", str(repo), "doctor"])

    output = capsys.readouterr().out
    assert code == 1
    assert "PASS gemini CLI: gemini 1.0" in output
    assert "FAIL docker daemon: daemon unavailable" in output
