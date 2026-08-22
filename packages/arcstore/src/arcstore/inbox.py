"""Storage-neutral inbox contracts.

An inbox is a durable communication view, not a projection of agent sessions.
The repository protocol speaks only in typed domain values so a PostgreSQL or
message-store implementation can be added without changing its consumers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, runtime_checkable
from uuid import uuid4

from arctrust.classification import parse_classification
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


class ParticipantRole(StrEnum):
    """Kinds of principals that can participate in a thread."""

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


class ReadState(StrEnum):
    """Explicit per-reader message state."""

    UNREAD = "unread"
    READ = "read"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Participant(_Contract):
    """A stable participant reference; display names are non-authoritative."""

    participant_id: str = Field(min_length=1)
    role: ParticipantRole
    display_name: str | None = Field(default=None, min_length=1)


class TraceMetadata(_Contract):
    """Classification-safe linkage metadata with no free-form payload."""

    trace_id: str | None = Field(default=None, min_length=1)
    parent_trace_id: str | None = Field(default=None, min_length=1)
    source_session_id: str | None = Field(default=None, min_length=1)
    handoff_id: str | None = Field(default=None, min_length=1)
    classification: str = "UNCLASSIFIED"

    @model_validator(mode="after")
    def _normalize_classification(self) -> Self:
        object.__setattr__(
            self,
            "classification",
            parse_classification(self.classification, strict=True).name,
        )
        return self


class Inbox(_Contract):
    """A participant-owned collection of communication threads."""

    inbox_id: str = Field(default_factory=lambda: _id("inbox"), min_length=1)
    owner: Participant
    classification: str = "UNCLASSIFIED"
    created_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _normalize_classification(self) -> Self:
        object.__setattr__(
            self,
            "classification",
            parse_classification(self.classification, strict=True).name,
        )
        return self


class Thread(_Contract):
    """A conversation with stable identity independent of execution sessions."""

    thread_id: str = Field(default_factory=lambda: _id("thread"), min_length=1)
    inbox_id: str = Field(min_length=1)
    participants: tuple[Participant, ...] = Field(min_length=1)
    subject: str | None = Field(default=None, min_length=1)
    classification: str = "UNCLASSIFIED"
    status: Literal["open", "closed"] = "open"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_message_id: str | None = None
    unread_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        normalized = parse_classification(self.classification, strict=True).name
        if len({item.participant_id for item in self.participants}) != len(self.participants):
            raise ValueError("thread participants must have unique IDs")
        object.__setattr__(self, "classification", normalized)
        return self


class ReadReceipt(_Contract):
    """The persisted read state for one participant."""

    participant_id: str = Field(min_length=1)
    state: ReadState = ReadState.UNREAD
    read_at: datetime | None = None


class Message(_Contract):
    """A message with reply and per-recipient read relationships."""

    message_id: str = Field(default_factory=lambda: _id("message"), min_length=1)
    thread_id: str = Field(min_length=1)
    sender: Participant
    recipients: tuple[Participant, ...] = Field(min_length=1)
    body: str = Field(min_length=1)
    reply_to_id: str | None = None
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
    created_at: datetime = Field(default_factory=_now)
    read_receipts: tuple[ReadReceipt, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if len({item.participant_id for item in self.recipients}) != len(self.recipients):
            raise ValueError("message recipients must have unique IDs")
        if len({item.participant_id for item in self.read_receipts}) != len(self.read_receipts):
            raise ValueError("message read receipts must have unique IDs")
        return self

    def state_for(self, participant_id: str) -> ReadState:
        """Return explicit state, defaulting to unread for a recipient."""
        for receipt in self.read_receipts:
            if receipt.participant_id == participant_id:
                return receipt.state
        return ReadState.UNREAD


class Handoff(_Contract):
    """A traceable transfer without arbitrary metadata."""

    handoff_id: str = Field(default_factory=lambda: _id("handoff"), min_length=1)
    thread_id: str = Field(min_length=1)
    from_participant: Participant
    to_participants: tuple[Participant, ...] = Field(min_length=1)
    source_message_id: str | None = None
    trace: TraceMetadata
    created_at: datetime = Field(default_factory=_now)
    status: Literal["pending", "accepted", "declined"] = "pending"

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if len({item.participant_id for item in self.to_participants}) != len(
            self.to_participants
        ):
            raise ValueError("handoff recipients must have unique IDs")
        return self


class PageInfo(_Contract):
    """Opaque cursor state for keyset pagination."""

    next_cursor: str | None = None
    has_more: bool = False


class ThreadPage(_Contract):
    items: tuple[Thread, ...]
    page_info: PageInfo


class MessagePage(_Contract):
    items: tuple[Message, ...]
    page_info: PageInfo


@runtime_checkable
class InboxRepository(Protocol):
    """Async storage seam; no database or driver types leak here.

    Implementations must authorize every reader against the relevant
    participants and enforce exact thread classification on messages and
    handoffs.  Sessions remain trace links, never thread identity.
    """

    async def create_inbox(
        self, owner: Participant, *, classification: str = "UNCLASSIFIED"
    ) -> Inbox: ...

    async def get_inbox(self, inbox_id: str) -> Inbox: ...

    async def create_thread(
        self,
        inbox_id: str,
        participants: tuple[Participant, ...],
        *,
        subject: str | None = None,
        classification: str = "UNCLASSIFIED",
    ) -> Thread: ...

    async def get_thread(
        self,
        thread_id: str,
        *,
        reader_id: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Thread: ...

    async def list_threads(
        self,
        inbox_id: str,
        *,
        reader_id: str,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> ThreadPage: ...

    async def append_message(
        self,
        thread_id: str,
        *,
        sender: Participant,
        recipients: tuple[Participant, ...],
        body: str,
        reply_to_id: str | None = None,
        trace: TraceMetadata | None = None,
    ) -> Message: ...

    async def list_messages(
        self,
        thread_id: str,
        *,
        reader_id: str,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> MessagePage: ...

    async def mark_read(self, message_id: str, reader_id: str) -> Message: ...

    async def create_handoff(
        self,
        thread_id: str,
        *,
        from_participant: Participant,
        to_participants: tuple[Participant, ...],
        source_message_id: str | None,
        trace: TraceMetadata,
    ) -> Handoff: ...

    async def list_handoffs(
        self, thread_id: str, *, reader_id: str, classification_max: str = "UNCLASSIFIED"
    ) -> tuple[Handoff, ...]: ...


__all__ = [
    "Handoff",
    "Inbox",
    "InboxRepository",
    "Message",
    "MessagePage",
    "PageInfo",
    "Participant",
    "ParticipantRole",
    "ReadReceipt",
    "ReadState",
    "Thread",
    "ThreadPage",
    "TraceMetadata",
]
