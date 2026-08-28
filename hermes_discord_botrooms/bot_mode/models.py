"""Small serializable value objects used by the room engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoomAttachment:
    path: str
    name: str
    kind: str = "file"
    mime_type: str = "application/octet-stream"

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True)
class RoomEvent:
    id: int
    event_uid: str
    room_id: str
    thread_id: str
    run_id: str
    kind: str
    author_kind: str
    author_id: str
    author_name: str
    text: str
    attachments: tuple[RoomAttachment, ...] = ()
    created_at: float = 0.0
    platform_message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEventCommit:
    status: str
    event: RoomEvent | None = None


@dataclass(frozen=True)
class MemberTurnResult:
    text: str = ""
    status: str = "complete"
    error: str = ""
    runtime_session_id: str = ""
    stored_session_id: str = ""


@dataclass(frozen=True)
class PendingPrompt:
    room_id: str
    run_id: str
    thread_id: str
    member_key: str
    kind: str
    request_id: str
    payload: dict[str, Any]
    runtime_session_id: str


@dataclass(frozen=True)
class SubmittedRun:
    room_id: str
    thread_id: str
    run_id: str
    event_id: int
    created: bool
    superseded_run_id: str = ""
