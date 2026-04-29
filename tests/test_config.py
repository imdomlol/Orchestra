from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from orch.config import load_config


def test_loads_default_config() -> None:
    config = load_config()

    assert config.models.orchestrator == "claude-opus"
    assert config.cli.gemini == "gemini"
    assert config.runtime.max_workers == 1
    assert config.sandbox.mode == "docker"
    assert config.sandbox.image == "orchestra-sandbox:py3.12"
    assert config.sandbox.dockerfile == "docker/orchestra-sandbox.Dockerfile"
    assert config.sandbox.build_context == "."
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
