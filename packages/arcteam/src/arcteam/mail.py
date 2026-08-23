"""Agent-to-agent mail composed by ArcTeam.

This module is the fleet-facing seam for the Agent Inbox.  ``MessagingService``
owns signed NATS delivery and ArcStore owns durable inbox persistence; this
facade composes them without making either lower layer know about the other.
Operator gateway sessions and channel chat never enter this service.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arcteam.types import DeliveryKind, Message


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


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


class AgentMailService:
    """Compose signed team transport with the durable ArcStore inbox adapter."""

    def __init__(self, transport: Any, store: MailStore) -> None:
        self._transport = transport
        self._store = store

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
        try:
            sent = await self._transport.send(envelope)
        except Exception:  # reason: durable record remains available for retry
            return MailSendResult(message_id=event_id, thread_id=thread_id, status="pending")
        return MailSendResult(message_id=sent.id, thread_id=thread_id, status="sent")

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
        copies = await self._store.record_event(
            event_id=event_id,
            sender=sender,
            recipients=recipients,
            body=body,
            external_thread_id=thread_id,
            reply_to_event_id=reply_to_id,
            trace=_trace(thread.classification),
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
        try:
            await self._transport.send(envelope)
        except Exception:  # reason: durable reply remains available for redelivery
            pass
        return copies[0]

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


__all__ = ["AgentMailService", "MailSendRequest", "MailSendResult", "mail_participant"]
