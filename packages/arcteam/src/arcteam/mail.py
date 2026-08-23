"""Agent-to-agent mail composed by ArcTeam.

This module is the fleet-facing seam for the Agent Inbox.  ``MessagingService``
owns signed NATS delivery and ArcStore owns durable inbox persistence; this
facade composes them without making either lower layer know about the other.
Operator gateway sessions and channel chat never enter this service.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arcteam.crypto import MessageSigner, new_nonce, sign_message
from arcteam.types import DeliveryKind, Message

_logger = logging.getLogger("arcteam.mail")


class MailSendRequest(BaseModel):
    """Validated mail input; all recipients are explicit agent/user addresses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sender: str = Field(min_length=1)
    sender_did: str = Field(min_length=1)
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

    async def record_event_with_outbox(self, **kwargs: Any) -> tuple[Any, ...]: ...

    async def get_thread(self, thread_id: str, **kwargs: Any) -> Any: ...

    async def get_message(self, message_id: str, **kwargs: Any) -> Any: ...

    async def list_threads(self, owner: Any, **kwargs: Any) -> Any: ...

    async def list_messages(self, thread_id: str, **kwargs: Any) -> Any: ...

    async def search(self, owner: Any, query: str, **kwargs: Any) -> Any: ...

    async def mark_read(self, message_id: str, *, reader: Any) -> Any: ...

    async def reply(self, *args: Any, **kwargs: Any) -> Any: ...

    async def create_handoff(self, *args: Any, **kwargs: Any) -> Any: ...

    async def list_handoffs(self, *args: Any, **kwargs: Any) -> Any: ...

    async def resolve_handoff(self, *args: Any, **kwargs: Any) -> Any: ...


class MailTransport(Protocol):
    async def send(self, message: Message) -> Message: ...


class MailAddressBook(Protocol):
    """Resolve transport addresses to the DIDs used by durable mail records."""

    async def did_for(self, address: str) -> str: ...

    async def address_for(self, did: str) -> str: ...


class RegistryMailAddressBook:
    """DID/address resolver backed by ArcTeam's public entity registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def did_for(self, address: str) -> str:
        from arcteam.registry import resolve_ref

        return resolve_ref(await self._registry.list_entities(), address)

    async def address_for(self, did: str) -> str:
        from arcteam.types import EntityType

        entity = await self._registry.get(did)
        if entity is None:
            raise ValueError(f"unknown mail participant: {did}")
        scheme = "agent" if entity.type is EntityType.AGENT else "user"
        return f"{scheme}://{entity.handle}"


class MailDeliveryWorker:
    """Drain durable mail envelopes with bounded exponential retry."""

    def __init__(
        self,
        outbox: Any,
        transport: MailTransport,
        *,
        worker_id: str,
        max_attempts: int = 5,
        on_dead_letter: Callable[[Any, Exception], Awaitable[None]] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._outbox = outbox
        self._transport = transport
        self._worker_id = worker_id
        self._max_attempts = max_attempts
        self._on_dead_letter = on_dead_letter

    async def deliver_once(self, *, limit: int = 100) -> tuple[str, ...]:
        delivered: list[str] = []
        claimed = self._outbox.claim(self._worker_id, limit=limit)
        if inspect.isawaitable(claimed):
            claimed = await claimed
        for entry in claimed:
            try:
                await self._transport.send(Message.model_validate(entry.envelope))
            except Exception as exc:  # reason: leave durable work pending
                if entry.attempts >= self._max_attempts:
                    result = self._outbox.dead_letter(
                        self._worker_id, entry.event_id, reason=type(exc).__name__
                    )
                    if inspect.isawaitable(result):
                        result = await result
                    if not result:
                        _logger.error("agent mail dead-letter lease lost: %s", entry.event_id)
                        continue
                    await self._notify_dead_letter(entry, exc)
                    continue
                delay = min(300.0, float(2 ** min(entry.attempts, 8)))
                result = self._outbox.nack(
                    self._worker_id, entry.event_id, retry_after_seconds=delay
                )
                if inspect.isawaitable(result):
                    result = await result
                if not result:
                    _logger.error("agent mail retry lease lost: %s", entry.event_id)
                    continue
                _logger.warning("agent mail delivery deferred: %s", type(exc).__name__)
            else:
                result = self._outbox.ack(self._worker_id, entry.event_id)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    delivered.append(entry.event_id)
                else:
                    _logger.error("agent mail acknowledgement lease lost: %s", entry.event_id)
        return tuple(delivered)

    async def _notify_dead_letter(self, entry: Any, error: Exception) -> None:
        if self._on_dead_letter is None:
            return
        try:
            await self._on_dead_letter(entry, error)
        except Exception:  # reason: terminal state is durable before advisory notification
            _logger.exception("agent mail dead-letter notification failed: %s", entry.event_id)


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
        outbox: Any,
        address_book: MailAddressBook,
        worker_id: str = "arc-team-mail",
        signer: MessageSigner,
    ) -> None:
        self._transport = transport
        self._store = store
        self._outbox = outbox
        self._address_book = address_book
        self._worker_id = worker_id
        self._signer = signer

    @property
    def sender_did(self) -> str:
        """Return the configured signing DID without exposing key material."""
        return self._signer.did

    async def send(self, request: MailSendRequest) -> MailSendResult:
        """Persist mail before NATS delivery and report delivery failures as pending."""
        if request.bcc:
            raise ValueError("bcc is not supported until recipient-private durable copies exist")
        if request.sender_did != self._signer.did:
            raise PermissionError("mail sender DID does not match the configured signer")
        recipients = (*request.to, *request.cc)
        recipient_dids = await _resolve_all(self._address_book, recipients)
        event_id = _stable_id("message", request.sender_did, request.idempotency_key)
        conversation_id = request.thread_id or _stable_id(
            "conversation", request.sender_did, request.idempotency_key
        )
        envelope = Message(
            id=event_id,
            sender=request.sender,
            to=list(request.to),
            cc=list(request.cc),
            delivery_kind=DeliveryKind.MAIL,
            subject=request.subject,
            body=request.body,
            thread_id=conversation_id,
            refs=list(request.attachments),
            attachments=list(request.attachments),
            idempotency_key=request.idempotency_key,
            classification=request.classification,
        )
        self._sign_envelope(envelope)
        projection = {
            "event_id": event_id,
            "sender": _participant(request.sender_did),
            "recipients": tuple(_participant(item) for item in recipient_dids),
            "body": request.body,
            "attachments": request.attachments,
            "external_thread_id": conversation_id,
            "subject": request.subject,
            "reply_to_event_id": request.reply_to_id,
            "trace": _trace(request.classification),
        }
        await self._store.record_event_with_outbox(
            **projection, envelope=envelope.model_dump(mode="json")
        )
        delivered = await MailDeliveryWorker(
            self._outbox, self._transport, worker_id=self._worker_id
        ).deliver_once()
        return MailSendResult(
            message_id=event_id,
            thread_id=conversation_id,
            status="sent" if event_id in delivered else "pending",
        )

    async def list_threads(self, owner: Any, **kwargs: Any) -> Any:
        return await self._store.list_threads(owner, **kwargs)

    async def list_messages(self, thread_id: str, *, reader: Any, **kwargs: Any) -> Any:
        return await self._store.list_messages(thread_id, reader=reader, **kwargs)

    async def search(self, owner: Any, query: str, **kwargs: Any) -> Any:
        return await self._store.search(owner, query, **kwargs)

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
        if sender.participant_id != self._signer.did:
            raise PermissionError("mail sender DID does not match the configured signer")
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
        conversation_id = thread.conversation_id
        if conversation_id is None:
            raise ValueError("mail thread has no canonical conversation identity")
        reply_to_event_id = None
        if reply_to_id is not None:
            parent = await self._store.get_message(
                reply_to_id,
                reader=sender,
                classification_max=classification_max,
            )
            if parent.thread_id != thread_id:
                raise ValueError("reply_to_id must reference the selected thread")
            if parent.event_id is None:
                raise ValueError("reply target has no canonical mail event identity")
            reply_to_event_id = parent.event_id
        event_id = _stable_id("message", conversation_id, sender.participant_id, idempotency_key)
        transport_recipients = await _resolve_addresses(self._address_book, recipients)
        envelope = Message(
            id=event_id,
            sender=sender.participant_id,
            to=list(transport_recipients),
            delivery_kind=DeliveryKind.MAIL,
            subject=thread.subject,
            body=body,
            thread_id=conversation_id,
            idempotency_key=idempotency_key,
            classification=thread.classification,
        )
        self._sign_envelope(envelope)
        copies = await self._store.record_event_with_outbox(
            event_id=event_id,
            sender=sender,
            recipients=recipients,
            body=body,
            external_thread_id=conversation_id,
            subject=thread.subject,
            reply_to_event_id=reply_to_event_id,
            trace=_trace(thread.classification),
            envelope=envelope.model_dump(mode="json"),
        )
        await MailDeliveryWorker(
            self._outbox, self._transport, worker_id=self._worker_id
        ).deliver_once()
        return copies[0]

    def _sign_envelope(self, envelope: Message) -> None:
        """Sign before persistence so an outbox edit cannot become new mail."""
        envelope.ts = envelope.ts or datetime.now(UTC).isoformat()
        envelope.signer_did = self._signer.did
        envelope.nonce = new_nonce()
        sign_message(envelope, self._signer.private_key)

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


async def _resolve_all(
    address_book: MailAddressBook, addresses: tuple[str, ...]
) -> tuple[str, ...]:
    resolved_items: list[str] = []
    for address in addresses:
        resolved_items.append(await address_book.did_for(address))
    resolved = tuple(resolved_items)
    if len(set(resolved)) != len(resolved):
        raise ValueError("mail recipients must resolve to distinct DIDs")
    return resolved


async def _resolve_addresses(
    address_book: MailAddressBook, participants: tuple[Any, ...]
) -> tuple[str, ...]:
    addresses: list[str] = []
    for participant in participants:
        addresses.append(await address_book.address_for(participant.participant_id))
    return tuple(addresses)


__all__ = [
    "AgentMailService",
    "MailAddressBook",
    "MailDeliveryWorker",
    "MailSendRequest",
    "MailSendResult",
    "RegistryMailAddressBook",
    "mail_participant",
]
