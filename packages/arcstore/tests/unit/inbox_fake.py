"""In-memory InboxRepository fake used only by the inbox contract tests."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import cast

from arctrust.classification import Classification, dominates, parse_classification

from arcstore.inbox import (
    Handoff,
    Inbox,
    InboxRepository,
    Message,
    MessagePage,
    PageInfo,
    Participant,
    ReadReceipt,
    ReadState,
    Thread,
    ThreadPage,
    TraceMetadata,
)


def _level(value: str) -> Classification:
    return parse_classification(value, strict=True)


def _now() -> datetime:
    return datetime.now(UTC)


class FakeInboxRepository:
    """Deterministic fake; production code must provide durable storage."""

    def __init__(self) -> None:
        self.inboxes: dict[str, Inbox] = {}
        self.threads: dict[str, Thread] = {}
        self.messages: dict[str, Message] = {}
        self.handoffs: dict[str, Handoff] = {}

    async def create_inbox(
        self,
        owner: Participant,
        *,
        classification: str = "UNCLASSIFIED",
        inbox_id: str | None = None,
    ) -> Inbox:
        if inbox_id is not None and inbox_id in self.inboxes:
            return self.inboxes[inbox_id]
        inbox = (
            Inbox(owner=owner, classification=classification, inbox_id=inbox_id)
            if inbox_id is not None
            else Inbox(owner=owner, classification=classification)
        )
        self.inboxes[inbox.inbox_id] = inbox
        return inbox

    async def get_inbox(self, inbox_id: str) -> Inbox:
        try:
            return self.inboxes[inbox_id]
        except KeyError as exc:
            raise KeyError(f"unknown inbox: {inbox_id}") from exc

    async def create_thread(
        self,
        inbox_id: str,
        participants: tuple[Participant, ...],
        *,
        subject: str | None = None,
        classification: str = "UNCLASSIFIED",
        thread_id: str | None = None,
    ) -> Thread:
        inbox = await self.get_inbox(inbox_id)
        if thread_id is not None and thread_id in self.threads:
            return self.threads[thread_id]
        thread = (
            Thread(
                inbox_id=inbox_id,
                participants=participants,
                subject=subject,
                classification=classification,
                thread_id=thread_id,
            )
            if thread_id is not None
            else Thread(
                inbox_id=inbox_id,
                participants=participants,
                subject=subject,
                classification=classification,
            )
        )
        if not dominates(_level(inbox.classification), _level(thread.classification)):
            raise ValueError("thread classification exceeds inbox classification")
        self.threads[thread.thread_id] = thread
        return thread

    async def get_thread(
        self,
        thread_id: str,
        *,
        reader_id: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Thread:
        thread = self._thread(thread_id)
        self._require_participant(thread, reader_id)
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
        self._validate_limit(limit)
        inbox = await self.get_inbox(inbox_id)
        if inbox.owner.participant_id != reader_id:
            raise PermissionError("reader must own the inbox")
        clearance = _level(classification_max)
        threads = sorted(
            (
                thread
                for thread in self.threads.values()
                if thread.inbox_id == inbox_id
                and reader_id in {item.participant_id for item in thread.participants}
                and _level(thread.classification) <= clearance
            ),
            key=lambda item: (item.updated_at, item.thread_id),
            reverse=True,
        )
        start = self._after_cursor(threads, cursor, inbox_id)
        selected = threads[start : start + limit]
        return ThreadPage(
            items=tuple(
                thread.model_copy(
                    update={"unread_count": self._unread_count(thread.thread_id, reader_id)}
                )
                for thread in selected
            ),
            page_info=self._page_info(threads, selected, start, limit, inbox_id),
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
        message_id: str | None = None,
    ) -> Message:
        thread = self._thread(thread_id)
        participants = {item.participant_id for item in thread.participants}
        if sender.participant_id not in participants or any(
            item.participant_id not in participants for item in recipients
        ):
            raise ValueError("sender and recipients must participate in the thread")
        if reply_to_id is not None:
            parent = self._message(reply_to_id)
            if parent.thread_id != thread_id:
                raise ValueError("reply_to_id must reference a message in this thread")
        if message_id is not None and message_id in self.messages:
            return self.messages[message_id]
        message = (
            Message(
                thread_id=thread_id,
                sender=sender,
                recipients=recipients,
                body=body,
                reply_to_id=reply_to_id,
                trace=trace or TraceMetadata(classification=thread.classification),
                message_id=message_id,
            )
            if message_id is not None
            else Message(
                thread_id=thread_id,
                sender=sender,
                recipients=recipients,
                body=body,
                reply_to_id=reply_to_id,
                trace=trace or TraceMetadata(classification=thread.classification),
            )
        )
        if message.trace.classification != thread.classification:
            raise ValueError("message classification must match thread classification")
        self.messages[message.message_id] = message
        self.threads[thread_id] = thread.model_copy(
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
        self._validate_limit(limit)
        thread = self._thread(thread_id)
        self._require_participant(thread, reader_id)
        clearance = _level(classification_max)
        if _level(thread.classification) > clearance:
            return MessagePage(items=(), page_info=PageInfo())
        messages = sorted(
            (
                message
                for message in self.messages.values()
                if message.thread_id == thread_id
                and (
                    message.sender.participant_id == reader_id
                    or reader_id in {item.participant_id for item in message.recipients}
                )
                and message.trace.classification == thread.classification
            ),
            key=lambda item: (item.created_at, item.message_id),
        )
        start = self._after_cursor(messages, cursor, thread_id)
        selected = messages[start : start + limit]
        return MessagePage(
            items=tuple(selected),
            page_info=self._page_info(messages, selected, start, limit, thread_id),
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
        self.messages[message_id] = updated
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
        participants = {item.participant_id for item in thread.participants}
        if from_participant.participant_id not in participants or any(
            item.participant_id not in participants for item in to_participants
        ):
            raise ValueError("handoff participants must belong to the thread")
        if (
            source_message_id is not None
            and self._message(source_message_id).thread_id != thread_id
        ):
            raise ValueError("source_message_id must reference a message in this thread")
        if trace.classification != thread.classification:
            raise ValueError("handoff classification must match thread classification")
        handoff = Handoff(
            thread_id=thread_id,
            from_participant=from_participant,
            to_participants=to_participants,
            source_message_id=source_message_id,
            trace=trace,
        )
        self.handoffs[handoff.handoff_id] = handoff
        return handoff

    async def list_handoffs(
        self, thread_id: str, *, reader_id: str, classification_max: str = "UNCLASSIFIED"
    ) -> tuple[Handoff, ...]:
        thread = self._thread(thread_id)
        self._require_participant(thread, reader_id)
        clearance = _level(classification_max)
        return tuple(
            handoff
            for handoff in self.handoffs.values()
            if handoff.thread_id == thread_id and _level(handoff.trace.classification) <= clearance
        )

    def _thread(self, thread_id: str) -> Thread:
        try:
            return self.threads[thread_id]
        except KeyError as exc:
            raise KeyError(f"unknown thread: {thread_id}") from exc

    def _message(self, message_id: str) -> Message:
        try:
            return self.messages[message_id]
        except KeyError as exc:
            raise KeyError(f"unknown message: {message_id}") from exc

    @staticmethod
    def _require_participant(thread: Thread, reader_id: str) -> None:
        if reader_id not in {item.participant_id for item in thread.participants}:
            raise PermissionError("reader must participate in the thread")

    def _unread_count(self, thread_id: str, reader_id: str) -> int:
        return sum(
            message.state_for(reader_id) is ReadState.UNREAD
            for message in self.messages.values()
            if message.thread_id == thread_id
            and reader_id in {item.participant_id for item in message.recipients}
        )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")

    @staticmethod
    def _encode(scope: str, item: Thread | Message) -> str:
        timestamp = item.updated_at if isinstance(item, Thread) else item.created_at
        identifier = item.thread_id if isinstance(item, Thread) else item.message_id
        payload = json.dumps(
            {"scope": scope, "timestamp": timestamp.isoformat(), "id": identifier}
        )
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def _decode(cursor: str, scope: str) -> tuple[datetime, str]:
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

    def _after_cursor(
        self, items: list[Thread] | list[Message], cursor: str | None, scope: str
    ) -> int:
        if cursor is None:
            return 0
        timestamp, identifier = self._decode(cursor, scope)
        for index, item in enumerate(items):
            if isinstance(item, Thread):
                key = (item.updated_at, item.thread_id)
            else:
                message = cast(Message, item)
                key = (message.created_at, message.message_id)
            if key == (timestamp, identifier):
                return index + 1
        raise ValueError("cursor no longer references an item in this page")

    def _page_info(
        self,
        all_items: list[Thread] | list[Message],
        selected: list[Thread] | list[Message],
        start: int,
        limit: int,
        scope: str,
    ) -> PageInfo:
        if start + len(selected) >= len(all_items) or not selected:
            return PageInfo()
        return PageInfo(next_cursor=self._encode(scope, selected[-1]), has_more=True)


assert isinstance(FakeInboxRepository(), InboxRepository)
