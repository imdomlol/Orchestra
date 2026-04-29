from __future__ import annotations

from pathlib import Path

import pytest

from orch.inbox import Inbox


def test_post_and_read_next_message(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)

    path = inbox.post("orchestrator", {"task_id": "T-0006", "action": "dispatch"})

    assert path.parent == tmp_path / ".orch/inbox/orchestrator"
    message = inbox.read_next("orchestrator")
    assert message is not None
    assert message.role == "orchestrator"
    assert message.body == {"task_id": "T-0006", "action": "dispatch"}


def test_messages_are_read_in_filename_order(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)
    role_dir = inbox.path_for("worker")
    role_dir.mkdir(parents=True)
    (role_dir / "20260101T000000000000Z-a.json").write_text(
        '{"sequence": 1}\n', encoding="utf-8"
    )
    (role_dir / "20260101T000000000001Z-b.json").write_text(
        '{"sequence": 2}\n', encoding="utf-8"
    )

    messages = inbox.list_messages("worker")

    assert [message.body["sequence"] for message in messages] == [1, 2]
    assert inbox.read_next("worker").body["sequence"] == 1


def test_message_is_delivered_until_acknowledged(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)
    inbox.post("orchestrator", {"action": "reject_plan"})

    first = inbox.read_next("orchestrator")
    second = inbox.read_next("orchestrator")

    assert first is not None
    assert second is not None
    assert first.path == second.path
    inbox.ack(first)
    assert inbox.read_next("orchestrator") is None


def test_rejects_unsafe_role_names(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)

    with pytest.raises(ValueError, match="invalid inbox role"):
        inbox.post("../orchestrator", {"action": "bad"})


def test_rejects_non_object_messages(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)

    with pytest.raises(TypeError, match="JSON object"):
        inbox.post("orchestrator", ["bad"])
