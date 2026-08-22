"""Idempotent projections of transport events into durable inboxes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from arcstore.inbox import (
    Handoff,
    Inbox,
    InboxRepository,
    Message,
    MessagePage,
    Participant,
    ParticipantRole,
    Thread,
    TraceMetadata,
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def participant(identifier: str, *, role: ParticipantRole = ParticipantRole.AGENT) -> Participant:
    """Make the minimal durable participant record for an Arc principal."""
    return Participant(participant_id=identifier, role=role)


class DurableInboxService:
    """Project one canonical message event into every participant's inbox."""

    def __init__(self, repository: InboxRepository) -> None:
        self._repository = repository

    async def inbox_for(
        self, owner: Participant, *, classification: str = "UNCLASSIFIED"
    ) -> Inbox:
        """Return the owner's durable inbox, creating it exactly once."""
        return await self._repository.create_inbox(
            owner,
            classification=classification,
            inbox_id=_stable_id("inbox", owner.participant_id),
        )

    async def record_event(
        self,
        *,
        event_id: str,
        sender: Participant,
        recipients: Iterable[Participant],
        body: str,
        external_thread_id: str | None = None,
        subject: str | None = None,
        reply_to_event_id: str | None = None,
        trace: TraceMetadata | None = None,
    ) -> tuple[Message, ...]:
        """Persist inbound or outbound delivery copies idempotently."""
        if not event_id:
            raise ValueError("event_id is required")
        recipient_list = tuple(recipients)
        if not recipient_list:
            raise ValueError("at least one recipient is required")
        all_participants = _unique((sender, *recipient_list))
        classification = trace.classification if trace is not None else "UNCLASSIFIED"
        copies: list[Message] = []
        for owner in _unique((sender, *recipient_list)):
            inbox = await self.inbox_for(owner, classification=classification)
            thread = await self._repository.create_thread(
                inbox.inbox_id,
                all_participants,
                subject=subject,
                classification=classification,
                thread_id=_stable_id("thread", inbox.inbox_id, external_thread_id or event_id),
            )
            reply_to_id = (
                _stable_id("message", inbox.inbox_id, reply_to_event_id)
                if reply_to_event_id is not None
                else None
            )
            copies.append(
                await self._repository.append_message(
                    thread.thread_id,
                    sender=sender,
                    recipients=recipient_list,
                    body=body,
                    reply_to_id=reply_to_id,
                    trace=trace or TraceMetadata(classification=classification),
                    message_id=_stable_id("message", inbox.inbox_id, event_id),
                )
            )
        return tuple(copies)

    async def list_threads(
        self,
        owner: Participant,
        *,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> tuple[Inbox, tuple[Thread, ...], str | None]:
        inbox = await self.inbox_for(owner, classification=classification_max)
        page = await self._repository.list_threads(
            inbox.inbox_id,
            reader_id=owner.participant_id,
            cursor=cursor,
            limit=limit,
            classification_max=classification_max,
        )
        return inbox, page.items, page.page_info.next_cursor

    async def list_messages(
        self,
        thread_id: str,
        *,
        reader: Participant,
        cursor: str | None = None,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> MessagePage:
        return await self._repository.list_messages(
            thread_id,
            reader_id=reader.participant_id,
            cursor=cursor,
            limit=limit,
            classification_max=classification_max,
        )

    async def mark_read(self, message_id: str, *, reader: Participant) -> Message:
        return await self._repository.mark_read(message_id, reader.participant_id)

    async def create_handoff(
        self,
        thread_id: str,
        *,
        sender: Participant,
        recipients: Iterable[Participant],
        source_message_id: str | None,
        trace: TraceMetadata,
    ) -> Handoff:
        return await self._repository.create_handoff(
            thread_id,
            from_participant=sender,
            to_participants=tuple(recipients),
            source_message_id=source_message_id,
            trace=trace,
        )

    async def list_handoffs(
        self,
        thread_id: str,
        *,
        reader: Participant,
        classification_max: str = "UNCLASSIFIED",
    ) -> tuple[Handoff, ...]:
        return await self._repository.list_handoffs(
            thread_id,
            reader_id=reader.participant_id,
            classification_max=classification_max,
        )


def _unique(items: Iterable[Participant]) -> tuple[Participant, ...]:
    unique: dict[str, Participant] = {}
    for item in items:
        unique.setdefault(item.participant_id, item)
    return tuple(unique.values())


__all__ = ["DurableInboxService", "participant"]
