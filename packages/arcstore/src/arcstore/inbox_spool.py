"""Crash-surviving retry journal for durable-inbox projections.

This is deliberately a small sibling of :mod:`arcstore.spool`: projection
writes are business records, so they cannot use the operational spool's closed
``SpoolKind`` vocabulary or its fail-open policy.  A newline-terminated JSON
record is appended before a repository write and acknowledged only after that
write succeeds.  Replaying after restart is idempotent because inbox event IDs
are deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arcstore.inbox import Participant, TraceMetadata
from arcstore.spool import read_complete_segments

_FILE_MODE = 0o600


class ProjectionEvent(BaseModel):
    """The complete, typed input required to retry one inbox projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    sender: Participant
    recipients: tuple[Participant, ...] = Field(min_length=1)
    body: str = Field(min_length=1)
    attachments: tuple[str, ...] = ()
    external_thread_id: str | None = None
    subject: str | None = None
    reply_to_event_id: str | None = None
    trace: TraceMetadata | None = None


class InboxProjectionSpool:
    """Append-only pending/ack journal with a restart-safe pending view."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def enqueue(self, event: ProjectionEvent) -> None:
        self._append({"operation": "enqueue", "event": event.model_dump(mode="json")})

    def acknowledge(self, event_id: str) -> None:
        self._append({"operation": "ack", "event_id": event_id})

    def pending(self) -> tuple[ProjectionEvent, ...]:
        records, _ = read_complete_segments(self._path, 0)
        queued: dict[str, ProjectionEvent] = {}
        for raw in records:
            try:
                item: Any = json.loads(raw)
                operation = item["operation"]
                if operation == "enqueue":
                    event = ProjectionEvent.model_validate(item["event"])
                    queued[event.event_id] = event
                elif operation == "ack" and isinstance(item["event_id"], str):
                    queued.pop(item["event_id"], None)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # A corrupt journal line must not hide later valid work.
                continue
        return tuple(queued.values())

    def _append(self, record: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, _FILE_MODE)
            os.write(descriptor, line.encode("utf-8"))
        finally:
            os.close(descriptor)


__all__ = ["InboxProjectionSpool", "ProjectionEvent"]
