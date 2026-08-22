"""Storage-neutral inbox contracts.

An inbox is a durable communication view, not a projection of agent sessions.
The repository protocol deliberately speaks only in typed domain values so a
PostgreSQL or message-store implementation can be added without changing the
dashboard or gateway contracts.  ``InMemoryInboxRepository`` is intentionally
test-only infrastructure; it is not selected by any production surface.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, TypeVar, runtime_checkable
from uuid import uuid4

from arctrust.classification import Classification, dominates, parse_classification
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
    def _normalize_classification(self) -> Self:
        object.__setattr__(
            self,
            "classification",
            parse_classification(self.classification, strict=True).name,
        )
        return self


class ReadReceipt(_Contract):
    """The persisted read state for one participant."""

    participant_id: str = Field(min_length=1)
    state: ReadState = ReadState.UNREAD
    read_at: datetime | None = None


class Message(_Contract):
    """A message in a thread, including its reply and read relationships."""

    message_id: str = Field(default_factory=lambda: _id("message"), min_length=1)
    thread_id: str = Field(min_length=1)
    sender: Participant
    recipients: tuple[Participant, ...] = Field(min_length=1)
    body: str = Field(min_length=1)
    reply_to_id: str | None = None
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
    created_at: datetime = Field(default_factory=_now)
    read_receipts: tuple[ReadReceipt, ...] = ()

    def state_for(self, participant_id: str) -> ReadState:
        """Return explicit state, defaulting to unread for a recipient."""
        for receipt in self.read_receipts:
            if receipt.participant_id == participant_id:
                return receipt.state
        return ReadState.UNREAD


class Handoff(_Contract):
    """A traceable transfer between participants without arbitrary metadata."""

    handoff_id: str = Field(default_factory=lambda: _id("handoff"), min_length=1)
    thread_id: str = Field(min_length=1)
    from_participant: Participant
    to_participants: tuple[Participant, ...] = Field(min_length=1)
    source_message_id: str | None = None
    trace: TraceMetadata
    created_at: datetime = Field(default_factory=_now)
    status: Literal["pending", "accepted", "declined"] = "pending"


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
    """Async storage seam for inboxes; no database or driver types leak here."""

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


class InMemoryInboxRepository:
    """Small deterministic fake for domain tests; never used as production storage."""

    def __init__(self) -> None:
        self._inboxes: dict[str, Inbox] = {}
        self._threads: dict[str, Thread] = {}
        self._messages: dict[str, Message] = {}
        self._handoffs: dict[str, Handoff] = {}

    async def create_inbox(
        self, owner: Participant, *, classification: str = "UNCLASSIFIED"
    ) -> Inbox:
        inbox = Inbox(owner=owner, classification=classification)
        self._inboxes[inbox.inbox_id] = inbox
        return inbox

    async def get_inbox(self, inbox_id: str) -> Inbox:
        try:
            return self._inboxes[inbox_id]
        except KeyError as exc:
            raise KeyError(f"unknown inbox: {inbox_id}") from exc

    async def create_thread(
        self,
        inbox_id: str,
        participants: tuple[Participant, ...],
        *,
        subject: str | None = None,
        classification: str = "UNCLASSIFIED",
    ) -> Thread:
        inbox = await self.get_inbox(inbox_id)
        thread = Thread(
            inbox_id=inbox_id,
            participants=participants,
            subject=subject,
            classification=classification,
        )
        if not dominates(_level(inbox.classification), _level(thread.classification)):
            raise ValueError("thread classification exceeds inbox classification")
        self._threads[thread.thread_id] = thread
        return thread

    async def get_thread(
        self,
        thread_id: str,
        *,
        reader_id: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Thread:
        thread = self._thread(thread_id)
        if reader_id not in {item.participant_id for item in thread.participants}:
            raise PermissionError("reader must participate in the thread")
        if _level(thread.classification) > _level(classification_max):
            raise PermissionError("reader clearance is insufficient for this thread")
        return thread.model_copy(update={"unread_count": self._unread_count(thread_id, reader_id)})

    async def list_threads(
        self,
        inbox_id: str,
        *,
        reader_id: str,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> ThreadPage:
        _validate_limit(limit)
        clearance = _level(classification_max)
        await self.get_inbox(inbox_id)
        threads = [
            thread
            for thread in self._threads.values()
            if thread.inbox_id == inbox_id and _level(thread.classification) <= clearance
        ]
        threads.sort(key=lambda item: (item.updated_at, item.thread_id), reverse=True)
        start = _after_cursor(threads, cursor, lambda item: (item.updated_at, item.thread_id))
        selected = threads[start : start + limit]
        return ThreadPage(
            items=tuple(
                thread.model_copy(
                    update={"unread_count": self._unread_count(thread.thread_id, reader_id)}
                )
                for thread in selected
            ),
            page_info=_page_info(threads, selected, start, limit, inbox_id),
        )

    async def append_message(
        self,
        thread_id: str,
        *,
        sender: Participant,
        recipients: tuple[Participant, ...],
        body: str,
        reply_to_id: str | None = None,
        trace: TraceMetadata | None = None,
    ) -> Message:
        thread = self._thread(thread_id)
        participant_ids = {item.participant_id for item in thread.participants}
        if sender.participant_id not in participant_ids or any(
            item.participant_id not in participant_ids for item in recipients
        ):
            raise ValueError("sender and recipients must participate in the thread")
        if reply_to_id is not None:
            parent = self._messages.get(reply_to_id)
            if parent is None or parent.thread_id != thread_id:
                raise ValueError("reply_to_id must reference a message in this thread")
        message = Message(
            thread_id=thread_id,
            sender=sender,
            recipients=recipients,
            body=body,
            reply_to_id=reply_to_id,
            trace=trace or TraceMetadata(classification=thread.classification),
        )
        if not dominates(_level(thread.classification), _level(message.trace.classification)):
            raise ValueError("message classification exceeds thread classification")
        self._messages[message.message_id] = message
        self._threads[thread_id] = thread.model_copy(
            update={"updated_at": message.created_at, "last_message_id": message.message_id}
        )
        return message

    async def list_messages(
        self,
        thread_id: str,
        *,
        reader_id: str,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> MessagePage:
        _validate_limit(limit)
        clearance = _level(classification_max)
        thread = self._thread(thread_id)
        if _level(thread.classification) > clearance:
            return MessagePage(items=(), page_info=PageInfo())
        messages = [
            message
            for message in self._messages.values()
            if message.thread_id == thread_id
            and reader_id in {item.participant_id for item in message.recipients}
            and _level(message.trace.classification) <= clearance
        ]
        messages.sort(key=lambda item: (item.created_at, item.message_id))
        start = _after_cursor(messages, cursor, lambda item: (item.created_at, item.message_id))
        selected = messages[start : start + limit]
        return MessagePage(
            items=tuple(selected),
            page_info=_page_info(messages, selected, start, limit, thread_id),
        )

    async def mark_read(self, message_id: str, reader_id: str) -> Message:
        message = self._message(message_id)
        if reader_id not in {item.participant_id for item in message.recipients}:
            raise ValueError("reader must be a message recipient")
        receipts = [
            receipt for receipt in message.read_receipts if receipt.participant_id != reader_id
        ]
        receipts.append(
            ReadReceipt(participant_id=reader_id, state=ReadState.READ, read_at=_now())
        )
        updated = message.model_copy(update={"read_receipts": tuple(receipts)})
        self._messages[message_id] = updated
        return updated

    async def create_handoff(
        self,
        thread_id: str,
        *,
        from_participant: Participant,
        to_participants: tuple[Participant, ...],
        source_message_id: str | None,
        trace: TraceMetadata,
    ) -> Handoff:
        thread = self._thread(thread_id)
        if source_message_id is not None:
            source = self._message(source_message_id)
            if source.thread_id != thread_id:
                raise ValueError("source_message_id must reference a message in this thread")
        participants = {item.participant_id for item in thread.participants}
        if from_participant.participant_id not in participants or any(
            item.participant_id not in participants for item in to_participants
        ):
            raise ValueError("handoff participants must belong to the thread")
        if not dominates(_level(thread.classification), _level(trace.classification)):
            raise ValueError("handoff classification exceeds thread classification")
        handoff = Handoff(
            thread_id=thread_id,
            from_participant=from_participant,
            to_participants=to_participants,
            source_message_id=source_message_id,
            trace=trace,
        )
        self._handoffs[handoff.handoff_id] = handoff
        return handoff

    async def list_handoffs(
        self, thread_id: str, *, reader_id: str, classification_max: str = "UNCLASSIFIED"
    ) -> tuple[Handoff, ...]:
        thread = self._thread(thread_id)
        if reader_id not in {item.participant_id for item in thread.participants}:
            raise ValueError("reader must participate in the thread")
        clearance = _level(classification_max)
        return tuple(
            handoff
            for handoff in self._handoffs.values()
            if handoff.thread_id == thread_id and _level(handoff.trace.classification) <= clearance
        )

    def _thread(self, thread_id: str) -> Thread:
        try:
            return self._threads[thread_id]
        except KeyError as exc:
            raise KeyError(f"unknown thread: {thread_id}") from exc

    def _message(self, message_id: str) -> Message:
        try:
            return self._messages[message_id]
        except KeyError as exc:
            raise KeyError(f"unknown message: {message_id}") from exc

    def _unread_count(self, thread_id: str, reader_id: str) -> int:
        return sum(
            message.state_for(reader_id) is ReadState.UNREAD
            for message in self._messages.values()
            if message.thread_id == thread_id
            and reader_id in {item.participant_id for item in message.recipients}
        )


def _level(value: str) -> Classification:
    return parse_classification(value, strict=True)


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")


def _encode_cursor(scope: str, key: tuple[datetime, str]) -> str:
    payload = json.dumps({"scope": scope, "timestamp": key[0].isoformat(), "id": key[1]})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, scope: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload["scope"] != scope:
            raise ValueError("cursor scope does not match query")
        return datetime.fromisoformat(payload["timestamp"]), payload["id"]
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("invalid cursor") from exc


_PageItem = TypeVar("_PageItem", Thread, Message)


def _after_cursor(
    items: list[_PageItem],
    cursor: str | None,
    key_fn: Callable[[_PageItem], tuple[datetime, str]],
) -> int:
    if cursor is None:
        return 0
    scope = ""
    if items:
        scope = items[0].thread_id if isinstance(items[0], Message) else items[0].inbox_id
    key = _decode_cursor(cursor, scope)
    for index, item in enumerate(items):
        if key_fn(item) == key:
            return index + 1
    return 0


def _page_info(
    all_items: list[_PageItem], selected: list[_PageItem], start: int, limit: int, scope: str
) -> PageInfo:
    has_more = start + len(selected) < len(all_items)
    if not has_more or not selected:
        return PageInfo(has_more=False)
    item = selected[-1]
    key = (
        (item.updated_at, item.thread_id)
        if isinstance(item, Thread)
        else (item.created_at, item.message_id)
    )
    return PageInfo(next_cursor=_encode_cursor(scope, key), has_more=True)


__all__ = [
    "Handoff",
    "InMemoryInboxRepository",
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
