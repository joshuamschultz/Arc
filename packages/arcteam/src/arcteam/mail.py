"""Agent-to-agent mail composed by ArcTeam.

This module is the fleet-facing seam for the Agent Inbox.  ``MessagingService``
owns signed NATS delivery and ArcStore owns durable inbox persistence; this
facade composes them without making either lower layer know about the other.
Operator gateway sessions and channel chat never enter this service.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arcstore.mail_outbox import MailOutbox

from arcteam.types import DeliveryKind, Message

_logger = logging.getLogger("arcteam.mail")


class MailSendRequest(BaseModel):
    """Validated mail input; all recipients are explicit agent/user addresses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sender: str = Field(min_length=1)
    to: tuple[str, ...] = Field(min_length=1)
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str | None = Field(default=None, min_length=1)
    body: str = Field(min_length=1)
    thread_id: str | None = None
    reply_to_id: str | None = None
    classification: str = "UNCLASSIFIED"
    attachments: tuple[str, ...] = ()
    idempotency_key: str = Field(min_length=1)


class MailSendResult(BaseModel):
    """Durability result returned by the facade, including an explicit pending state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    thread_id: str
    status: str


class MailStore(Protocol):
    async def record_event(self, **kwargs: Any) -> tuple[Any, ...]: ...

    async def get_thread(self, thread_id: str, **kwargs: Any) -> Any: ...

    async def list_threads(self, owner: Any, **kwargs: Any) -> Any: ...

    async def list_messages(self, thread_id: str, **kwargs: Any) -> Any: ...

    async def mark_read(self, message_id: str, *, reader: Any) -> Any: ...

    async def reply(self, *args: Any, **kwargs: Any) -> Any: ...

    async def create_handoff(self, *args: Any, **kwargs: Any) -> Any: ...

    async def list_handoffs(self, *args: Any, **kwargs: Any) -> Any: ...

    async def resolve_handoff(self, *args: Any, **kwargs: Any) -> Any: ...


class MailTransport(Protocol):
    async def send(self, message: Message) -> Message: ...


class MailDeliveryWorker:
    """Drain durable mail envelopes with bounded exponential retry."""

    def __init__(self, outbox: MailOutbox, transport: MailTransport, *, worker_id: str) -> None:
        self._outbox = outbox
        self._transport = transport
        self._worker_id = worker_id

    async def deliver_once(self, *, limit: int = 100) -> tuple[str, ...]:
        delivered: list[str] = []
        for entry in self._outbox.claim(self._worker_id, limit=limit):
            try:
                await self._transport.send(Message.model_validate(entry.envelope))
            except Exception as exc:  # reason: leave durable work pending
                delay = min(300.0, float(2 ** min(entry.attempts, 8)))
                self._outbox.nack(
                    self._worker_id, entry.event_id, retry_after_seconds=delay
                )
                _logger.warning("agent mail delivery deferred: %s", type(exc).__name__)
            else:
                self._outbox.ack(self._worker_id, entry.event_id)
                delivered.append(entry.event_id)
        return tuple(delivered)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


class AgentMailService:
    """Compose signed team transport with the durable ArcStore inbox adapter."""

    def __init__(
        self,
        transport: MailTransport,
        store: MailStore,
        *,
        outbox: MailOutbox | None = None,
        worker_id: str = "arc-team-mail",
    ) -> None:
        self._transport = transport
        self._store = store
        self._outbox = outbox
        self._worker_id = worker_id

    async def send(self, request: MailSendRequest) -> MailSendResult:
        """Persist mail before NATS delivery and report delivery failures as pending."""
        if request.bcc:
            raise ValueError("bcc is not supported until recipient-private durable copies exist")
        recipients = (*request.to, *request.cc)
        event_id = _stable_id("message", request.sender, request.idempotency_key)
        thread_id = request.thread_id or _stable_id(
            "thread", request.sender, request.idempotency_key
        )
        await self._store.record_event(
            event_id=event_id,
            sender=_participant(request.sender),
            recipients=tuple(_participant(item) for item in recipients),
            body=request.body,
            attachments=request.attachments,
            external_thread_id=thread_id,
            subject=request.subject,
            reply_to_event_id=request.reply_to_id,
            trace=_trace(request.classification),
        )
        envelope = Message(
            id=event_id,
            sender=request.sender,
            to=list(request.to),
            cc=list(request.cc),
            delivery_kind=DeliveryKind.MAIL,
            subject=request.subject,
            body=request.body,
            thread_id=thread_id,
            refs=list(request.attachments),
            attachments=list(request.attachments),
            idempotency_key=request.idempotency_key,
            classification=request.classification,
        )
        if self._outbox is None:
            try:
                sent = await self._transport.send(envelope)
            except Exception:  # reason: durable record remains available for retry
                return MailSendResult(message_id=event_id, thread_id=thread_id, status="pending")
            return MailSendResult(message_id=sent.id, thread_id=thread_id, status="sent")
        self._outbox.enqueue(event_id, envelope.model_dump(mode="json"))
        delivered = await MailDeliveryWorker(
            self._outbox, self._transport, worker_id=self._worker_id
        ).deliver_once()
        return MailSendResult(
            message_id=event_id,
            thread_id=thread_id,
            status="sent" if event_id in delivered else "pending",
        )

    async def list_threads(self, owner: Any, **kwargs: Any) -> Any:
        return await self._store.list_threads(owner, **kwargs)

    async def list_messages(self, thread_id: str, *, reader: Any, **kwargs: Any) -> Any:
        return await self._store.list_messages(thread_id, reader=reader, **kwargs)

    async def mark_read(self, message_id: str, *, reader: Any) -> Any:
        return await self._store.mark_read(message_id, reader=reader)

    async def reply(
        self,
        thread_id: str,
        *,
        sender: Any,
        body: str,
        reply_to_id: str | None,
        idempotency_key: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Any:
        thread = await self._store.get_thread(
            thread_id,
            reader_id=sender.participant_id,
            classification_max=classification_max,
        )
        recipients = tuple(
            item for item in thread.participants if item.participant_id != sender.participant_id
        )
        if not recipients:
            raise ValueError("a reply requires another participant")
        event_id = _stable_id("message", thread_id, sender.participant_id, idempotency_key)
        message = await self._store.reply(
            thread_id,
            sender=sender,
            body=body,
            reply_to_id=reply_to_id,
            idempotency_key=idempotency_key,
            classification_max=classification_max,
        )
        envelope = Message(
            id=event_id,
            sender=sender.participant_id,
            to=[item.participant_id for item in recipients],
            delivery_kind=DeliveryKind.MAIL,
            subject=thread.subject,
            body=body,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            classification=thread.classification,
        )
        if self._outbox is not None:
            self._outbox.enqueue(event_id, envelope.model_dump(mode="json"))
            await MailDeliveryWorker(
                self._outbox, self._transport, worker_id=self._worker_id
            ).deliver_once()
        else:
            try:
                await self._transport.send(envelope)
            except Exception as exc:  # reason: durable reply remains available for redelivery
                _logger.warning("agent mail reply delivery pending: %s", type(exc).__name__)
        return message

    async def create_handoff(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.create_handoff(*args, **kwargs)

    async def list_handoffs(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.list_handoffs(*args, **kwargs)

    async def resolve_handoff(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.resolve_handoff(*args, **kwargs)


def _participant(identifier: str) -> Any:
    from arcstore.inbox_projection import participant

    return participant(identifier)


def mail_participant(identifier: str) -> Any:
    """Return the canonical participant model for ArcUI/CLI composition."""
    return _participant(identifier)


def _trace(classification: str) -> Any:
    from arcstore.inbox import TraceMetadata

    return TraceMetadata(classification=classification)


__all__ = [
    "AgentMailService",
    "MailDeliveryWorker",
    "MailSendRequest",
    "MailSendResult",
    "mail_participant",
]
