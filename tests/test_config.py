from __future__ import annotations

from pathlib import Path
import shutil
import shlex
import sys

import pytest

from orch.config import load_config


def test_loads_default_config() -> None:
    config = load_config()

    assert config.models.orchestrator == "claude-opus"
    assert config.cli.gemini == shlex.join((sys.executable, "-m", "orch.gemini_sdk_runner"))
    assert config.runtime.max_workers == 1
    assert config.runtime.poll_interval_seconds == 2
    assert config.sandbox.mode == "docker"
    assert config.sandbox.image == "orchestra-sandbox:py3.12"
    assert config.sandbox.dockerfile == "docker/orchestra-sandbox.Dockerfile"
    assert config.sandbox.build_context == "."
    assert config.budgets.max_tasks_per_request == 5
    assert config.budgets.max_wall_clock_minutes == 60
    assert config.critic.mode == "opus"
    assert config.chat.model == "claude-opus-4-7"
    assert config.delegate_always.enabled is True
    assert config.delegate_always.chat_model == "claude-opus-4-7"
    assert config.delegate_always.planner_model == "gemini"
    assert config.delegate_always.worker_model == "codex-gpt-5.5"
    assert config.delegate_always.reviewer_model == "codex-gpt-5.5"
    assert config.delegate_always.claude_reads_files == "on_attention"
    assert config.delegate_always.claude_reads_worker_logs == "on_failure"
    assert config.delegate_always.gemini_interface == "sdk_single_call"
    assert config.delegate_always.max_tasks_small_repo == 1
    assert config.delegate_always.max_tasks_default == 3
    assert config.delegate_always.max_parallel_workers == 2
    assert ".git/**" in config.policies.forbidden_globs


def test_rejects_invalid_worker_count(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    orchestrator_path = config_dir / "orchestrator.toml"
    text = orchestrator_path.read_text(encoding="utf-8")
    orchestrator_path.write_text(text.replace("max_workers = 1", "max_workers = 0"))

    with pytest.raises(ValueError, match="max_workers"):
        load_config(config_dir)


def test_rejects_missing_policies(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    (config_dir / "policies.toml").unlink()

    with pytest.raises(FileNotFoundError, match="policies.toml"):
        load_config(config_dir)


def test_rejects_invalid_critic_mode(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    orchestrator_path = config_dir / "orchestrator.toml"
    text = orchestrator_path.read_text(encoding="utf-8")
    orchestrator_path.write_text(text.replace('mode = "opus"', 'mode = "nope"'))

    with pytest.raises(ValueError, match="critic.mode"):
        load_config(config_dir)


def test_delegate_mode_defaults_when_section_missing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    orchestrator_path = config_dir / "orchestrator.toml"
    text = orchestrator_path.read_text(encoding="utf-8")
    text = text[: text.index("\n[mode.delegate_always]")]
    orchestrator_path.write_text(text, encoding="utf-8")

    config = load_config(config_dir)

    assert config.delegate_always.enabled is True
    assert config.delegate_always.claude_reads_files == "on_attention"
    assert config.delegate_always.claude_reads_worker_logs == "on_failure"
    assert config.delegate_always.gemini_interface == "sdk_single_call"
    assert config.delegate_always.max_tasks_small_repo == 1
    assert config.delegate_always.max_tasks_default == 3
    assert config.delegate_always.max_parallel_workers == 2


def test_loads_configured_delegate_mode(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    orchestrator_path = config_dir / "orchestrator.toml"
    text = orchestrator_path.read_text(encoding="utf-8")
    text = text.replace("enabled = true", "enabled = false")
    text = text.replace('chat_model = "claude-opus-4-7"', 'chat_model = "claude-test"')
    text = text.replace('planner_model = "gemini"', 'planner_model = "gemini-test"')
    text = text.replace('worker_model = "codex-gpt-5.5"', 'worker_model = "codex-worker-test"')
    text = text.replace(
        'reviewer_model = "codex-gpt-5.5"',
        'reviewer_model = "codex-reviewer-test"',
    )
    text = text.replace('claude_reads_files = "on_attention"', 'claude_reads_files = "never"')
    text = text.replace(
        'claude_reads_worker_logs = "on_failure"',
        'claude_reads_worker_logs = "always"',
    )
    text = text.replace('gemini_interface = "sdk_single_call"', 'gemini_interface = "cli"')
    text = text.replace("max_tasks_small_repo = 1", "max_tasks_small_repo = 2")
    text = text.replace("max_tasks_default = 3", "max_tasks_default = 4")
    text = text.replace("max_parallel_workers = 2", "max_parallel_workers = 5")
    orchestrator_path.write_text(text, encoding="utf-8")

    config = load_config(config_dir)

    assert config.delegate_always.enabled is False
    assert config.delegate_always.chat_model == "claude-test"
    assert config.delegate_always.planner_model == "gemini-test"
    assert config.delegate_always.worker_model == "codex-worker-test"
    assert config.delegate_always.reviewer_model == "codex-reviewer-test"
    assert config.delegate_always.claude_reads_files == "never"
    assert config.delegate_always.claude_reads_worker_logs == "always"
    assert config.delegate_always.gemini_interface == "cli"
    assert config.delegate_always.max_tasks_small_repo == 2
    assert config.delegate_always.max_tasks_default == 4
    assert config.delegate_always.max_parallel_workers == 5


def test_rejects_invalid_delegate_read_policy(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    orchestrator_path = config_dir / "orchestrator.toml"
    text = orchestrator_path.read_text(encoding="utf-8")
    orchestrator_path.write_text(
        text.replace('claude_reads_files = "on_attention"', 'claude_reads_files = "often"'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mode.delegate_always.claude_reads_files"):
        load_config(config_dir)


def test_rejects_non_positive_delegate_task_cap(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    shutil.copytree(".orch/config", config_dir)
    orchestrator_path = config_dir / "orchestrator.toml"
    text = orchestrator_path.read_text(encoding="utf-8")
    orchestrator_path.write_text(
        text.replace("max_tasks_default = 3", "max_tasks_default = 0"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mode.delegate_always.max_tasks_default"):
        load_config(config_dir)
