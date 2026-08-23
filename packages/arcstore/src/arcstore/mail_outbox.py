"""Durable, idempotent outbox for Agent Mail transport envelopes."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arcstore.spool import read_complete_segments

_FILE_MODE = 0o600


class MailOutboxEntry(BaseModel):
    """One deterministic transport attempt persisted before NATS."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    envelope: dict[str, Any]
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MailOutbox:
    """Crash-safe JSONL outbox with claim/ack/nack semantics.

    The journal is intentionally append-only.  A restart reconstructs the
    latest state for each deterministic event id; an enqueue is idempotent and
    an expired lease becomes available to a new worker.
    """

    def __init__(self, path: Path, *, lease_seconds: float = 60.0) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._path = path
        self._lease_seconds = lease_seconds

    def enqueue(self, event_id: str, envelope: dict[str, Any]) -> MailOutboxEntry:
        state, _leases = self._snapshot()
        existing = state.get(event_id)
        if existing is not None:
            return existing
        entry = MailOutboxEntry(event_id=event_id, envelope=dict(envelope))
        self._append({"operation": "enqueue", "entry": entry.model_dump(mode="json")})
        return entry

    def claim(self, consumer_id: str, *, limit: int = 100) -> tuple[MailOutboxEntry, ...]:
        if not consumer_id:
            raise ValueError("consumer_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")
        now = datetime.now(UTC)
        entries, leases = self._snapshot()
        claimed: list[MailOutboxEntry] = []
        for entry in entries.values():
            if len(claimed) >= limit:
                break
            if entry.available_at > now:
                continue
            lease = leases.get(entry.event_id)
            if lease is not None and lease[1] > now:
                continue
            claimed.append(entry)
            self._append(
                {
                    "operation": "claim",
                    "event_id": entry.event_id,
                    "consumer_id": consumer_id,
                    "lease_until": (now + timedelta(seconds=self._lease_seconds)).isoformat(),
                    "attempts": entry.attempts + 1,
                }
            )
            leases[entry.event_id] = (
                consumer_id,
                now + timedelta(seconds=self._lease_seconds),
            )
        return tuple(
            item.model_copy(update={"attempts": item.attempts + 1}) for item in claimed
        )

    def ack(self, consumer_id: str, event_id: str) -> bool:
        lease = self._lease(event_id)
        if lease is None or lease[0] != consumer_id:
            return False
        self._append({"operation": "ack", "event_id": event_id, "consumer_id": consumer_id})
        return True

    def nack(self, consumer_id: str, event_id: str, *, retry_after_seconds: float) -> bool:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        lease = self._lease(event_id)
        if lease is None or lease[0] != consumer_id:
            return False
        available = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        self._append(
            {
                "operation": "nack",
                "event_id": event_id,
                "consumer_id": consumer_id,
                "available_at": available.isoformat(),
            }
        )
        return True

    def pending(self) -> tuple[MailOutboxEntry, ...]:
        return tuple(self._snapshot()[0].values())

    def _lease(self, event_id: str) -> tuple[str, datetime] | None:
        records, _ = read_complete_segments(self._path, 0)
        owner: str | None = None
        lease_until: datetime | None = None
        for raw in records:
            try:
                item = json.loads(raw)
                if item.get("event_id") != event_id:
                    continue
                operation = item.get("operation")
                if operation == "claim":
                    owner = str(item["consumer_id"])
                    lease_until = datetime.fromisoformat(item["lease_until"])
                elif operation == "ack":
                    owner, lease_until = None, None
                elif operation == "nack":
                    lease_until = datetime.now(UTC)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        if owner is None or lease_until is None:
            return None
        return owner, lease_until

    def _snapshot(
        self,
    ) -> tuple[dict[str, MailOutboxEntry], dict[str, tuple[str, datetime]]]:
        records, _ = read_complete_segments(self._path, 0)
        entries: dict[str, MailOutboxEntry] = {}
        leases: dict[str, tuple[str, datetime]] = {}
        for raw in records:
            try:
                item = json.loads(raw)
                operation = item["operation"]
                event_id = item.get("event_id")
                if operation == "enqueue":
                    entry = MailOutboxEntry.model_validate(item["entry"])
                    entries.setdefault(entry.event_id, entry)
                elif isinstance(event_id, str) and event_id in entries:
                    if operation == "claim":
                        leases[event_id] = (
                            str(item["consumer_id"]),
                            datetime.fromisoformat(item["lease_until"]),
                        )
                        entries[event_id] = entries[event_id].model_copy(
                            update={"attempts": int(item["attempts"])}
                        )
                    elif operation == "nack":
                        entries[event_id] = entries[event_id].model_copy(
                            update={"available_at": datetime.fromisoformat(item["available_at"])}
                        )
                        leases.pop(event_id, None)
                    elif operation == "ack":
                        entries.pop(event_id, None)
                        leases.pop(event_id, None)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return entries, leases

    def _append(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, _FILE_MODE)
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["MailOutbox", "MailOutboxEntry"]
