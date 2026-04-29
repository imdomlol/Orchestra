from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.validate_task import validate_task


def test_example_task_is_valid() -> None:
    validate_task(Path("examples/task.example.yaml"))


def test_invalid_status_is_rejected(tmp_path: Path) -> None:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text())
    task["status"] = "done-ish"
    task_path = tmp_path / "bad-task.yaml"
    task_path.write_text(yaml.safe_dump(task), encoding="utf-8")

    with pytest.raises(ValueError, match="status"):
        validate_task(task_path)


def test_missing_owned_files_is_rejected(tmp_path: Path) -> None:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text())
    task["owned_files"] = []
    task_path = tmp_path / "bad-task.yaml"
    task_path.write_text(yaml.safe_dump(task), encoding="utf-8")

    with pytest.raises(ValueError, match="owned_files"):
        validate_task(task_path)
