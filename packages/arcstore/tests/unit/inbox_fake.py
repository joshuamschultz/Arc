"""In-memory InboxRepository fake used only by the inbox contract tests."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from arctrust.classification import Classification, dominates, parse_classification

from arcstore.inbox import (
    Handoff,
    HandoffStatus,
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
        self.mail_outbox: dict[str, dict[str, object]] = {}

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
        conversation_id: str | None = None,
    ) -> Thread:
        inbox = await self.get_inbox(inbox_id)
        if thread_id is not None and thread_id in self.threads:
            existing = self.threads[thread_id]
            if {item.participant_id for item in existing.participants} != {
                item.participant_id for item in participants
            }:
                raise ValueError("existing mail thread has different participants")
            if existing.classification != classification:
                raise ValueError("existing mail thread has different classification")
            if existing.subject != subject:
                raise ValueError("existing mail thread has different subject")
            if existing.conversation_id != conversation_id:
                raise ValueError("existing mail thread has different conversation identity")
            return existing
        thread = (
            Thread(
                inbox_id=inbox_id,
                participants=participants,
                subject=subject,
                classification=classification,
                thread_id=thread_id,
                conversation_id=conversation_id,
            )
            if thread_id is not None
            else Thread(
                inbox_id=inbox_id,
                participants=participants,
                subject=subject,
                classification=classification,
                conversation_id=conversation_id,
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

    async def get_message(
        self,
        message_id: str,
        *,
        reader_id: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Message:
        message = self._message(message_id)
        thread = self._thread(message.thread_id)
        self._require_participant(thread, reader_id)
        if _level(thread.classification) > _level(classification_max):
            raise PermissionError("reader clearance is insufficient for this message")
        if not (
            message.sender.participant_id == reader_id
            or reader_id in {item.participant_id for item in message.recipients}
        ):
            raise PermissionError("reader cannot access this message")
        return message

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
        attachments: tuple[str, ...] = (),
        reply_to_id: str | None = None,
        event_id: str | None = None,
        reply_to_event_id: str | None = None,
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
                attachments=attachments,
                reply_to_id=reply_to_id,
                event_id=event_id,
                reply_to_event_id=reply_to_event_id,
                trace=trace or TraceMetadata(classification=thread.classification),
                message_id=message_id,
            )
            if message_id is not None
            else Message(
                thread_id=thread_id,
                sender=sender,
                recipients=recipients,
                body=body,
                attachments=attachments,
                reply_to_id=reply_to_id,
                event_id=event_id,
                reply_to_event_id=reply_to_event_id,
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

    async def record_event_with_outbox(
        self,
        *,
        event_id: str,
        sender: Participant,
        recipients: tuple[Participant, ...],
        body: str,
        attachments: tuple[str, ...] = (),
        external_thread_id: str | None = None,
        subject: str | None = None,
        reply_to_event_id: str | None = None,
        trace: TraceMetadata | None = None,
        envelope: dict[str, object],
    ) -> tuple[Message, ...]:
        snapshot = (
            copy.deepcopy(self.inboxes),
            copy.deepcopy(self.threads),
            copy.deepcopy(self.messages),
            copy.deepcopy(self.mail_outbox),
        )
        try:
            copies: list[Message] = []
            classification = trace.classification if trace else "UNCLASSIFIED"
            all_participants = tuple(dict.fromkeys((sender, *recipients)))
            for owner in all_participants:
                inbox_id = "inbox_" + hashlib.sha256(owner.participant_id.encode()).hexdigest()
                inbox = await self.create_inbox(
                    owner, classification=classification, inbox_id=inbox_id
                )
                thread_id = (
                    "thread_"
                    + hashlib.sha256(
                        f"{inbox_id}\x1f{external_thread_id or event_id}".encode()
                    ).hexdigest()
                )
                thread = await self.create_thread(
                    inbox.inbox_id,
                    all_participants,
                    subject=subject,
                    classification=classification,
                    thread_id=thread_id,
                    conversation_id=external_thread_id or event_id,
                )
                message_id = (
                    "message_"
                    + hashlib.sha256(f"{inbox.inbox_id}\x1f{event_id}".encode()).hexdigest()
                )
                copies.append(
                    await self.append_message(
                        thread.thread_id,
                        sender=sender,
                        recipients=recipients,
                        body=body,
                        attachments=attachments,
                        reply_to_id=(
                            "message_"
                            + hashlib.sha256(
                                f"{inbox.inbox_id}\x1f{reply_to_event_id}".encode()
                            ).hexdigest()
                            if reply_to_event_id
                            else None
                        ),
                        event_id=event_id,
                        reply_to_event_id=reply_to_event_id,
                        trace=trace,
                        message_id=message_id,
                    )
                )
            self.mail_outbox.setdefault(event_id, dict(envelope))
            return tuple(copies)
        except Exception:
            self.inboxes, self.threads, self.messages, self.mail_outbox = snapshot
            raise

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
        handoff_id: str | None = None,
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
        if handoff_id is not None and handoff_id in self.handoffs:
            return self.handoffs[handoff_id]
        fields = {
            "thread_id": thread_id,
            "from_participant": from_participant,
            "to_participants": to_participants,
            "source_message_id": source_message_id,
            "trace": trace,
        }
        handoff = (
            Handoff(handoff_id=handoff_id, **fields)
            if handoff_id is not None
            else Handoff(**fields)
        )
        self.handoffs[handoff.handoff_id] = handoff
        return handoff

    async def resolve_handoff(
        self,
        handoff_id: str,
        *,
        recipient: Participant,
        actor_did: str,
        status: HandoffStatus,
    ) -> Handoff:
        handoff = self.handoffs[handoff_id]
        if recipient.participant_id not in {
            item.participant_id for item in handoff.to_participants
        }:
            raise PermissionError("only an addressed recipient can resolve a handoff")
        if not actor_did.startswith("did:"):
            raise ValueError("handoff resolution actor must be a DID")
        if handoff.status is not HandoffStatus.PENDING:
            if (
                handoff.status is status
                and handoff.resolved_by == recipient
                and handoff.resolved_actor_did == actor_did
            ):
                return handoff
            raise ValueError("handoff is already resolved")
        if status is HandoffStatus.PENDING:
            raise ValueError("handoff must be accepted or declined")
        updated = handoff.model_copy(
            update={
                "status": status,
                "resolved_by": recipient,
                "resolved_actor_did": actor_did,
                "resolved_at": _now(),
            }
        )
        self.handoffs[handoff_id] = updated
        return updated

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
