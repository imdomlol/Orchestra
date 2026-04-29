from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from orch.images import SandboxImageBuilder


def copy_config(repo: Path) -> None:
    shutil.copytree(".orch/config", repo / ".orch/config")


def test_build_argv_uses_configured_project_image(tmp_path: Path) -> None:
    copy_config(tmp_path)

    argv = SandboxImageBuilder.from_config(root=tmp_path).build_argv()

    assert argv == (
        "docker",
        "build",
        "--tag",
        "orchestra-sandbox:py3.12",
        "--file",
        str(tmp_path / "docker/orchestra-sandbox.Dockerfile"),
        str(tmp_path),
    )


def test_build_argv_can_include_rebuild_flags(tmp_path: Path) -> None:
    copy_config(tmp_path)

    argv = SandboxImageBuilder.from_config(root=tmp_path).build_argv(
        no_cache=True,
        pull=True,
    )

    assert "--no-cache" in argv
    assert "--pull" in argv
    assert argv[-1] == str(tmp_path)


def test_build_argv_rejects_paths_outside_repo(tmp_path: Path) -> None:
    copy_config(tmp_path)
    config_path = tmp_path / ".orch/config/orchestrator.toml"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace(
            'dockerfile = "docker/orchestra-sandbox.Dockerfile"',
            'dockerfile = "../Dockerfile"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside repo root"):
        SandboxImageBuilder.from_config(root=tmp_path).build_argv()
