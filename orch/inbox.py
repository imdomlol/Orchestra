"""Durable JSON inboxes for cross-agent messages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4


ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class InboxMessage:
    role: str
    path: Path
    body: dict[str, Any]

    @property
    def id(self) -> str:
        return self.path.stem


class Inbox:
    def __init__(self, root: Path = Path(".")) -> None:
        self.root = root
        self.inbox_root = root / ".orch" / "inbox"

    def post(self, role: str, body: dict[str, Any]) -> Path:
        if not isinstance(body, dict):
            raise TypeError("message body must be a JSON object")

        role_dir = self.path_for(role)
        role_dir.mkdir(parents=True, exist_ok=True)
        final_path = role_dir / f"{self._message_id()}.json"
        temp_path = final_path.with_suffix(".json.tmp")

        payload = json.dumps(body, indent=2, sort_keys=True) + "\n"
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        self._fsync_dir(role_dir)
        return final_path

    def list_messages(self, role: str) -> list[InboxMessage]:
        role_dir = self.path_for(role)
        if not role_dir.exists():
            return []
        return [self.read_path(path) for path in sorted(role_dir.glob("*.json"))]

    def read_next(self, role: str) -> InboxMessage | None:
        messages = self.list_messages(role)
        if not messages:
            return None
        return messages[0]

    def read_path(self, path: Path) -> InboxMessage:
        resolved_path = path.resolve()
        try:
            relative = resolved_path.relative_to(self.inbox_root.resolve())
        except ValueError as exc:
            raise ValueError(f"message path is outside inbox: {path}") from exc
        if len(relative.parts) != 2 or relative.suffix != ".json":
            raise ValueError(f"invalid message path: {path}")

        role = relative.parts[0]
        self._validate_role(role)
        with resolved_path.open("r", encoding="utf-8") as handle:
            body = json.load(handle)
        if not isinstance(body, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return InboxMessage(role=role, path=resolved_path, body=body)

    def ack(self, message: InboxMessage | Path) -> None:
        path = message.path if isinstance(message, InboxMessage) else message
        resolved_path = path.resolve()
        try:
            relative = resolved_path.relative_to(self.inbox_root.resolve())
        except ValueError as exc:
            raise ValueError(f"message path is outside inbox: {path}") from exc
        if len(relative.parts) != 2 or relative.suffix != ".json":
            raise ValueError(f"invalid message path: {path}")

        resolved_path.unlink(missing_ok=True)
        self._fsync_dir(resolved_path.parent)

    def path_for(self, role: str) -> Path:
        self._validate_role(role)
        path = (self.inbox_root / role).resolve()
        try:
            path.relative_to(self.inbox_root.resolve())
        except ValueError as exc:
            raise ValueError(f"unsafe inbox path: {path}") from exc
        return path

    def _validate_role(self, role: str) -> None:
        if not ROLE_RE.fullmatch(role):
            raise ValueError(f"invalid inbox role: {role}")

    def _message_id(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}-{os.getpid()}-{uuid4().hex}"

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
