"""Shared Pydantic models and type definitions for ArcTeam messaging."""

from __future__ import annotations

import itertools
import re
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EntityType(StrEnum):
    """Type of registered entity."""

    AGENT = "agent"
    USER = "user"


class MsgType(StrEnum):
    """Message classification type."""

    INFO = "info"
    REQUEST = "request"
    TASK = "task"
    RESULT = "result"
    ALERT = "alert"
    ACK = "ack"


class Priority(StrEnum):
    """Message priority level."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# URI scheme pattern: scheme://name
_URI_PATTERN = re.compile(r"^(agent|user|channel|role)://([a-zA-Z0-9_-]+)$")

# Valid URI schemes
VALID_SCHEMES = frozenset({"agent", "user", "channel", "role"})


def parse_uri(uri: str) -> tuple[str, str]:
    """Parse a messaging URI into (scheme, name).

    Supports: agent://, user://, channel://, role://

    Raises:
        ValueError: If URI is malformed or uses an unknown scheme.
    """
    match = _URI_PATTERN.match(uri)
    if not match:
        raise ValueError(
            f"Invalid URI: {uri!r}. Expected scheme://name where scheme is one of {VALID_SCHEMES}"
        )
    return match.group(1), match.group(2)


def make_uri(scheme: str, name: str) -> str:
    """Build a URI from scheme and name."""
    if scheme not in VALID_SCHEMES:
        raise ValueError(f"Invalid scheme: {scheme!r}. Must be one of {VALID_SCHEMES}")
    return f"{scheme}://{name}"


MAX_BODY_BYTES = 65536  # 64KB


class Message(BaseModel):
    """13-field message envelope. Maps to NATS JetStream message."""

    seq: int = 0
    id: str = ""
    ts: str = ""
    sender: str
    to: list[str]
    thread_id: str | None = None
    msg_type: MsgType = MsgType.INFO
    priority: Priority = Priority.NORMAL
    action_required: bool = False
    body: str
    refs: list[str] = Field(default_factory=list)
    status: str = "sent"
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body")
    @classmethod
    def validate_body_size(cls, v: str) -> str:
        """Enforce 64KB body size limit."""
        if len(v.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError(f"Message body exceeds {MAX_BODY_BYTES} bytes (64KB limit)")
        return v


_msg_counter = itertools.count()


def generate_message_id() -> str:
    """Generate a unique message ID: msg_{timestamp}_{counter}."""
    return f"msg_{time.time_ns()}_{next(_msg_counter):06d}"


class Entity(BaseModel):
    """Registered agent or user."""

    id: str
    name: str
    type: EntityType
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    created: str = ""
    status: str = "active"


class Channel(BaseModel):
    """Channel definition."""

    name: str
    description: str = ""
    members: list[str] = Field(default_factory=list)
    created: str = ""


class Cursor(BaseModel):
    """Per-entity read position in a stream."""

    consumer: str
    stream: str
    seq: int = 0
    byte_pos: int = 0
    updated_at: str = ""


class AuditRecord(BaseModel):
    """Tamper-evident audit entry."""

    audit_seq: int
    event_type: str
    stream: str = ""
    msg_seq: int | None = None
    subject: str
    actor_id: str
    target_id: str | None = None
    classification: str = "UNCLASSIFIED"
    timestamp_utc: str
    detail: str
    hmac_sha256: str = ""
