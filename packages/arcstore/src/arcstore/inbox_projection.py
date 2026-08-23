"""Idempotent projections of transport events into durable inboxes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from arcstore.inbox import (
    Handoff,
    HandoffStatus,
    Inbox,
    InboxRepository,
    Message,
    MessagePage,
    Participant,
    ParticipantRole,
    Thread,
    TraceMetadata,
)
from arcstore.inbox_spool import InboxProjectionSpool, ProjectionEvent


@runtime_checkable
class InboxDeliveryPort(Protocol):
    """Explicit application-owned delivery seam for durable inbox effects.

    The port receives deterministic ids.  Implementations must therefore make
    their own external sends idempotent (a retry may arrive after a crash).
    ArcStore never imports a gateway or the team bus.
    """

    async def deliver_reply(self, message: Message) -> None: ...

    async def wake_handoff(self, handoff: Handoff) -> None: ...

    async def deliver_handoff_resolution(self, handoff: Handoff) -> None: ...


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def participant(identifier: str, *, role: ParticipantRole = ParticipantRole.AGENT) -> Participant:
    """Make the minimal durable participant record for an Arc principal."""
    return Participant(participant_id=identifier, role=role)


class DurableInboxService:
    """Project one canonical message event into every participant's inbox."""

    def __init__(
        self,
        repository: InboxRepository,
        *,
        delivery_port: InboxDeliveryPort | None = None,
        projection_spool: InboxProjectionSpool | None = None,
    ) -> None:
        self._repository = repository
        self._delivery_port = delivery_port
        self._projection_spool = projection_spool

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
        attachments: Iterable[str] = (),
        external_thread_id: str | None = None,
        subject: str | None = None,
        reply_to_event_id: str | None = None,
        trace: TraceMetadata | None = None,
    ) -> tuple[Message, ...]:
        """Persist inbound or outbound delivery copies idempotently."""
        event = ProjectionEvent(
            event_id=event_id,
            sender=sender,
            recipients=tuple(recipients),
            body=body,
            attachments=tuple(attachments),
            external_thread_id=external_thread_id,
            subject=subject,
            reply_to_event_id=reply_to_event_id,
            trace=trace,
        )
        if self._projection_spool is not None:
            self._projection_spool.enqueue(event)
        copies = await self._record_projection(event)
        if self._projection_spool is not None:
            self._projection_spool.acknowledge(event.event_id)
        return copies

    async def record_event_with_outbox(self, **kwargs: object) -> tuple[Message, ...]:
        """Atomically persist inbox copies and one signed transport envelope."""
        method = getattr(self._repository, "record_event_with_outbox", None)
        if method is None:
            raise RuntimeError("atomic inbox/outbox repository seam is unavailable")
        return await method(**kwargs)

    async def retry_pending_projections(self) -> tuple[str, ...]:
        """Replay all persisted but unacknowledged projections after an outage/restart."""
        if self._projection_spool is None:
            return ()
        completed: list[str] = []
        for event in self._projection_spool.pending():
            await self._record_projection(event)
            self._projection_spool.acknowledge(event.event_id)
            completed.append(event.event_id)
        return tuple(completed)

    async def _record_projection(self, event: ProjectionEvent) -> tuple[Message, ...]:
        """Apply one already-durable projection input to the repository."""
        event_id = event.event_id
        sender = event.sender
        recipient_list = event.recipients
        body = event.body
        attachments = event.attachments
        external_thread_id = event.external_thread_id
        subject = event.subject
        reply_to_event_id = event.reply_to_event_id
        trace = event.trace
        if not event_id:
            raise ValueError("event_id is required")
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
            if reply_to_event_id is None:
                reply_to_id = None
            elif reply_to_event_id.startswith("message_"):
                reply_to_id = reply_to_event_id
            else:
                reply_to_id = _stable_id("message", inbox.inbox_id, reply_to_event_id)
            copies.append(
                await self._repository.append_message(
                    thread.thread_id,
                    sender=sender,
                    recipients=recipient_list,
                    body=body,
                    attachments=attachments,
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

    async def search(
        self,
        owner: Participant,
        query: str,
        *,
        limit: int = 50,
        classification_max: str = "UNCLASSIFIED",
    ) -> tuple[Message, ...]:
        """Search only messages in threads the owner is authorized to read."""
        needle = query.strip().casefold()
        if not needle:
            raise ValueError("search query is required")
        _, threads, _ = await self.list_threads(
            owner, limit=max(limit, 100), classification_max=classification_max
        )
        matches: list[Message] = []
        for thread in threads:
            page = await self.list_messages(
                thread.thread_id,
                reader=owner,
                limit=100,
                classification_max=classification_max,
            )
            for message in page.items:
                if needle in message.body.casefold() or needle in (
                    thread.subject or ""
                ).casefold():
                    matches.append(message)
                    if len(matches) >= limit:
                        return tuple(matches)
        return tuple(matches)

    async def get_thread(
        self,
        thread_id: str,
        *,
        reader_id: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Thread:
        """Read one authorized mail thread without exposing session state."""
        return await self._repository.get_thread(
            thread_id,
            reader_id=reader_id,
            classification_max=classification_max,
        )

    async def mark_read(self, message_id: str, *, reader: Participant) -> Message:
        return await self._repository.mark_read(message_id, reader.participant_id)

    async def reply(
        self,
        thread_id: str,
        *,
        sender: Participant,
        body: str,
        reply_to_id: str | None = None,
        idempotency_key: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Message:
        """Append an authorized reply from one thread participant.

        The durable write happens before the explicit recipient dispatch.  If
        dispatch fails, retrying with the same key returns the same stored
        message and asks the port to send that same deterministic delivery.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if self._delivery_port is None:
            raise RuntimeError("inbox delivery port is unavailable")
        thread = await self._repository.get_thread(
            thread_id,
            reader_id=sender.participant_id,
            classification_max=classification_max,
        )
        recipients = tuple(
            item for item in thread.participants if item.participant_id != sender.participant_id
        )
        if not recipients:
            raise ValueError("a reply requires at least one other thread participant")
        message = await self._repository.append_message(
            thread_id,
            sender=sender,
            recipients=recipients,
            body=body,
            reply_to_id=reply_to_id,
            trace=TraceMetadata(classification=thread.classification),
            message_id=_stable_id("message", thread_id, sender.participant_id, idempotency_key),
        )
        await self._delivery_port.deliver_reply(message)
        return message

    async def create_handoff(
        self,
        thread_id: str,
        *,
        sender: Participant,
        recipients: Iterable[Participant],
        source_message_id: str | None,
        trace: TraceMetadata,
        idempotency_key: str,
    ) -> Handoff:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if self._delivery_port is None:
            raise RuntimeError("inbox delivery port is unavailable")
        handoff = await self._repository.create_handoff(
            thread_id,
            from_participant=sender,
            to_participants=tuple(recipients),
            source_message_id=source_message_id,
            trace=trace,
            handoff_id=_stable_id("handoff", thread_id, sender.participant_id, idempotency_key),
        )
        await self._delivery_port.wake_handoff(handoff)
        return handoff

    async def resolve_handoff(
        self,
        handoff_id: str,
        *,
        recipient: Participant,
        status: HandoffStatus,
    ) -> Handoff:
        """Allow only an addressed recipient to accept or decline a pending handoff."""
        if status is HandoffStatus.PENDING:
            raise ValueError("a handoff must be accepted or declined")
        if self._delivery_port is None:
            raise RuntimeError("inbox delivery port is unavailable")
        handoff = await self._repository.resolve_handoff(
            handoff_id, recipient=recipient, status=status
        )
        await self._delivery_port.deliver_handoff_resolution(handoff)
        return handoff

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


__all__ = ["DurableInboxService", "InboxDeliveryPort", "participant"]
