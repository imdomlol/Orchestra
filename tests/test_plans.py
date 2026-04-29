from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from orch.plans import PlanIngestor, extract_task_blocks
from orch.task_store import TaskStore


def copy_runtime_layout(repo: Path) -> None:
    for directory in [
        ".orch/tasks/pending",
        ".orch/tasks/active",
        ".orch/tasks/done",
        ".orch/plans",
        ".orch/locks",
        ".orch/schemas",
    ]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy(".orch/schemas/task.schema.json", repo / ".orch/schemas/task.schema.json")


def load_task(task_id: str = "T-0001") -> dict:
    task = yaml.safe_load(Path("examples/task.example.yaml").read_text(encoding="utf-8"))
    task["id"] = task_id
    task["branch"] = f"task/{task_id}"
    task["worktree_path"] = f".orch/worktrees/{task_id}"
    return task


def plan_with_tasks(*tasks: dict) -> str:
    blocks = []
    for task in tasks:
        blocks.append(f"```yaml\n{yaml.safe_dump(task, sort_keys=False)}```")
    return "# Plan\n\n" + "\n\n".join(blocks)


def test_extract_task_blocks_ignores_non_task_yaml() -> None:
    markdown = """
```yaml
not_a_task: true
```

```yaml
id: T-0001
objective: Do the thing.
```
"""

    assert extract_task_blocks(markdown) == [{"id": "T-0001", "objective": "Do the thing."}]


def test_ingest_writes_embedded_tasks_to_pending(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    plan_path = tmp_path / ".orch/plans/P-0001.md"
    plan_path.write_text(plan_with_tasks(load_task("T-0001"), load_task("T-0002")), encoding="utf-8")

    result = PlanIngestor(tmp_path).ingest(".orch/plans/P-0001.md")

    assert result.task_count == 2
    assert result.task_paths == (
        tmp_path / ".orch/tasks/pending/T-0001.yaml",
        tmp_path / ".orch/tasks/pending/T-0002.yaml",
    )
    assert TaskStore(tmp_path).read("T-0002")["status"] == "pending"


def test_ingest_rejects_duplicate_tasks(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    store = TaskStore(tmp_path)
    store.write_pending(load_task("T-0001"))
    plan_path = tmp_path / ".orch/plans/P-0001.md"
    plan_path.write_text(plan_with_tasks(load_task("T-0001")), encoding="utf-8")

    with pytest.raises(FileExistsError, match="T-0001"):
        PlanIngestor(tmp_path).ingest(plan_path)


def test_ingest_validates_all_tasks_before_writing_any(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)
    bad_task = load_task("T-0002")
    bad_task.pop("objective")
    plan_path = tmp_path / ".orch/plans/P-0001.md"
    plan_path.write_text(plan_with_tasks(load_task("T-0001"), bad_task), encoding="utf-8")

    with pytest.raises(ValueError, match="objective"):
        PlanIngestor(tmp_path).ingest(plan_path)

    assert TaskStore(tmp_path).list_tasks("pending") == []


def test_ingest_rejects_plan_path_outside_repo(tmp_path: Path) -> None:
    copy_runtime_layout(tmp_path)

    with pytest.raises(ValueError, match="outside repo root"):
        PlanIngestor(tmp_path).ingest(tmp_path.parent / "P-0001.md")
