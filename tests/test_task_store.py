from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from orch.task_store import ReviewNote, TaskStore


def copy_runtime_layout(tmp_path: Path) -> None:
    for directory in [
        ".orch/tasks/pending",
        ".orch/tasks/active",
        ".orch/tasks/done",
        ".orch/locks",
        ".orch/schemas",
    ]:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy(".orch/schemas/task.schema.json", tmp_path / ".orch/schemas/task.schema.json")


def load_example() -> dict:
    return yaml.safe_load(Path("examples/task.example.yaml").read_text(encoding="utf-8"))


def test_write_and_read_pending_task(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    store = TaskStore(tmp_path)
    task = load_example()

    path = store.write_pending(task)

    assert path == tmp_path / ".orch/tasks/pending/T-0001.yaml"
    assert store.read("T-0001")["objective"] == task["objective"]


def test_claim_next_pending_moves_to_active(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    store = TaskStore(tmp_path)
    store.write_pending(load_example())

    active_path = store.claim_next_pending()

    assert active_path == tmp_path / ".orch/tasks/active/T-0001.yaml"
    assert active_path.exists()
    assert not (tmp_path / ".orch/tasks/pending/T-0001.yaml").exists()
    assert store.read("T-0001")["status"] == "in_progress"


def test_append_review_note_validates_note(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    store = TaskStore(tmp_path)
    store.write_pending(load_example())

    store.append_review_note(
        "T-0001",
        ReviewNote(
            author="claude-orchestrator",
            verdict="note",
            body="Ready for dispatch.",
        ),
    )

    task = store.read("T-0001")
    assert task["review_notes"][-1]["body"] == "Ready for dispatch."


def test_invalid_transition_is_rejected(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    store = TaskStore(tmp_path)
    store.write_pending(load_example())

    with pytest.raises(ValueError, match="not allowed"):
        store.transition("T-0001", "done", "in_progress")
