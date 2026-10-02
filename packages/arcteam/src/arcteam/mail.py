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

#: Reply target scheme for a turn opened by mail: ``mail://<conversation id>``.
MAIL_TARGET_SCHEME = "mail://"

#: Mail is one message plus at most this many replies. Conversation belongs in
#: the team channel, where the operator and the whole team can see it.
MAIL_REPLY_LIMIT = 1

#: The page size used to count a conversation's messages. Far above the two a
#: conversation may ever hold, so one page already proves a violation.
_THREAD_SCAN_LIMIT = 100

MAIL_THREAD_CLOSED = (
    "Mail allows one message and one reply, and this thread already has its reply. "
    "Continue in the team channel (messaging_send to channel://<name>), where the "
    "operator and the whole team can see it, or use create_task / assign_task for "
    "directed work."
)


class MailThreadClosedError(ValueError):
    """A reply to a reply, or a second reply: the conversation has ended.

    The message is written for the agent that hit it, so it continues the
    conversation where conversation belongs instead of retrying the mail.
    """

    def __init__(self, message: str = MAIL_THREAD_CLOSED) -> None:
        super().__init__(message)


def mail_target(conversation_id: str) -> str:
    """Return the reply target for a turn opened by mail in ``conversation_id``."""
    return f"{MAIL_TARGET_SCHEME}{conversation_id}"


def conversation_of(target: str | None) -> str | None:
    """Return the conversation a ``mail://`` reply target names, else ``None``."""
    if not target or not target.startswith(MAIL_TARGET_SCHEME):
        return None
    conversation = target[len(MAIL_TARGET_SCHEME) :]
    return conversation or None


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
        signer_did: str | None = None,
        max_attempts: int = 5,
        on_dead_letter: Callable[[Any, Exception], Awaitable[None]] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        # The transport re-signs every envelope it sends, so a worker drains
        # only the mail its own identity sealed. Draining a peer's envelope
        # would re-sign it under the wrong key and the recipient would
        # quarantine it as a forged origin (the reply would never arrive).
        self._signer_did = signer_did
        self._outbox = outbox
        self._transport = transport
        self._worker_id = worker_id
        self._max_attempts = max_attempts
        self._on_dead_letter = on_dead_letter

    async def deliver_once(self, *, limit: int = 100) -> tuple[str, ...]:
        delivered: list[str] = []
        claimed = self._outbox.claim(self._worker_id, limit=limit, signer_did=self._signer_did)
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

    def delivery_worker(self, worker_id: str | None = None) -> MailDeliveryWorker:
        """A worker that drains only the envelopes this identity signed."""
        return MailDeliveryWorker(
            self._outbox,
            self._transport,
            worker_id=worker_id or self._worker_id,
            signer_did=self._signer.did,
        )

    async def send(self, request: MailSendRequest) -> MailSendResult:
        """Persist mail before NATS delivery and report delivery failures as pending."""
        if request.bcc:
            raise ValueError("bcc is not supported until recipient-private durable copies exist")
        if request.sender_did != self._signer.did:
            raise PermissionError("mail sender DID does not match the configured signer")
        to = await _transport_addresses(self._address_book, request.to)
        cc = await _transport_addresses(self._address_book, request.cc)
        recipient_dids = await _resolve_all(self._address_book, (*to, *cc))
        event_id = _stable_id("message", request.sender_did, request.idempotency_key)
        conversation_id = request.thread_id or _stable_id(
            "conversation", request.sender_did, request.idempotency_key
        )
        if request.thread_id is not None:
            await self._require_open(
                _thread_id_for(request.sender_did, conversation_id),
                reader=_participant(request.sender_did),
                event_id=event_id,
                classification_max=request.classification,
                missing_ok=True,
            )
        envelope = Message(
            id=event_id,
            sender=request.sender,
            to=list(to),
            cc=list(cc),
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
        delivered = await self.delivery_worker().deliver_once()
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
        observed_as: Any | None = None,
    ) -> Any:
        """Post the conversation's one reply from ``sender``.

        ``observed_as`` is the participant whose copy an operator is viewing. A
        human operator who is not a participant joins the conversation through
        it; an agent never can (``PermissionError``). Either way the reply obeys
        the one-reply rule (:class:`MailThreadClosedError`).
        """
        if sender.participant_id != self._signer.did:
            raise PermissionError("mail sender DID does not match the configured signer")
        thread, reader, joining = await self._thread_for_reply(
            thread_id, sender, observed_as, classification_max
        )
        recipients = tuple(
            item for item in thread.participants if item.participant_id != sender.participant_id
        )
        if not recipients:
            raise ValueError("a reply requires another participant")
        conversation_id = thread.conversation_id
        if conversation_id is None:
            raise ValueError("mail thread has no canonical conversation identity")
        event_id = _stable_id("message", conversation_id, sender.participant_id, idempotency_key)
        # Count as the copy's owner: a joining operator sees only the messages
        # addressed to it, but the one-reply rule is about the whole conversation.
        await self._require_open(
            thread.thread_id,
            reader=_copy_owner(thread, conversation_id, reader),
            event_id=event_id,
            classification_max=classification_max,
        )
        reply_to_event_id = None
        if reply_to_id is not None:
            parent = await self._store.get_message(
                reply_to_id,
                reader=reader,
                classification_max=classification_max,
            )
            if parent.thread_id != thread.thread_id:
                raise ValueError("reply_to_id must reference the selected thread")
            if parent.event_id is None:
                raise ValueError("reply target has no canonical mail event identity")
            reply_to_event_id = parent.event_id
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
        record: dict[str, Any] = {
            "event_id": event_id,
            "sender": sender,
            "recipients": recipients,
            "body": body,
            "external_thread_id": conversation_id,
            "subject": thread.subject,
            "reply_to_event_id": reply_to_event_id,
            "trace": _trace(thread.classification),
            "envelope": envelope.model_dump(mode="json"),
        }
        if joining:
            record["join"] = True
        copies = await self._store.record_event_with_outbox(**record)
        await self.delivery_worker().deliver_once()
        return copies[0]

    async def reply_to_conversation(
        self,
        conversation_id: str,
        *,
        body: str,
        idempotency_key: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> Any:
        """Reply as this service's own identity into ``conversation_id``.

        The agent side of a mail turn knows the transport conversation id, not
        its own durable copy's id; this addresses that copy directly.
        """
        return await self.reply(
            _thread_id_for(self._signer.did, conversation_id),
            sender=_participant(self._signer.did),
            body=body,
            reply_to_id=None,
            idempotency_key=idempotency_key,
            classification_max=classification_max,
        )

    async def has_reply(
        self,
        conversation_id: str,
        *,
        idempotency_key: str,
        classification_max: str = "UNCLASSIFIED",
    ) -> bool:
        """Whether this identity's reply under ``idempotency_key`` is already durable."""
        event_id = _stable_id("message", conversation_id, self._signer.did, idempotency_key)
        try:
            events = await self._events(
                _thread_id_for(self._signer.did, conversation_id),
                reader=_participant(self._signer.did),
                classification_max=classification_max,
            )
        except KeyError:
            return False
        return event_id in events

    async def conversation_participants(
        self, conversation_id: str, *, classification_max: str = "UNCLASSIFIED"
    ) -> tuple[str, ...]:
        """Return the DIDs taking part in this identity's copy of a conversation."""
        thread = await self._store.get_thread(
            _thread_id_for(self._signer.did, conversation_id),
            reader_id=self._signer.did,
            classification_max=classification_max,
        )
        return tuple(item.participant_id for item in thread.participants)

    async def check_inbound(
        self, message: Message, *, classification_max: str = "UNCLASSIFIED"
    ) -> None:
        """Refuse an inbound mail envelope that breaks the one-reply rule.

        Run by the recipient before the mail wakes a turn. ``KeyError`` means
        the envelope was never made durable through this service (a raw bus
        publish); :class:`MailThreadClosedError` means it is a third message in
        its conversation (a forged or racing second reply).
        """
        conversation_id = message.thread_id or message.id
        events = list(
            await self._events(
                _thread_id_for(self._signer.did, conversation_id),
                reader=_participant(self._signer.did),
                classification_max=classification_max,
            )
        )
        if message.id not in events:
            if len(events) > MAIL_REPLY_LIMIT:
                raise MailThreadClosedError()
            raise KeyError(f"mail {message.id} is not in the durable conversation")
        if events.index(message.id) > MAIL_REPLY_LIMIT:
            raise MailThreadClosedError()

    async def _thread_for_reply(
        self,
        thread_id: str,
        sender: Any,
        observed_as: Any | None,
        classification_max: str,
    ) -> tuple[Any, Any, bool]:
        """Read the thread as the sender, or as the observed copy for an operator join."""
        try:
            thread = await self._store.get_thread(
                thread_id,
                reader_id=sender.participant_id,
                classification_max=classification_max,
            )
        except PermissionError:
            if observed_as is None or str(getattr(sender, "role", "")) != "human":
                raise
        else:
            return thread, sender, False
        thread = await self._store.get_thread(
            thread_id,
            reader_id=observed_as.participant_id,
            classification_max=classification_max,
        )
        return thread, observed_as, True

    async def _events(
        self, thread_id: str, *, reader: Any, classification_max: str
    ) -> dict[str, None]:
        """Return the conversation's event ids, oldest first (an ordered set)."""
        page = await self._store.list_messages(
            thread_id,
            reader=reader,
            limit=_THREAD_SCAN_LIMIT,
            classification_max=classification_max,
        )
        return {(item.event_id or item.message_id): None for item in page.items}

    async def _require_open(
        self,
        thread_id: str,
        *,
        reader: Any,
        event_id: str,
        classification_max: str,
        missing_ok: bool = False,
    ) -> None:
        """Enforce one message plus at most one reply; a replay of either passes."""
        try:
            events = await self._events(
                thread_id, reader=reader, classification_max=classification_max
            )
        except KeyError:
            if missing_ok:
                return
            raise
        if event_id not in events and len(events) > MAIL_REPLY_LIMIT:
            raise MailThreadClosedError()

    def _sign_envelope(self, envelope: Message) -> None:
        """Sign before persistence so an outbox edit cannot become new mail."""
        envelope.ts = envelope.ts or datetime.now(UTC).isoformat()
        envelope.signer_did = self._signer.did
        envelope.nonce = new_nonce()
        sign_message(envelope, self._signer.private_key)

    async def announce_handoff(self, handoff: Any) -> MailSendResult:
        """Wake a handoff's recipients with one signed mail naming the thread."""
        thread = await self._store.get_thread(
            handoff.thread_id,
            reader_id=self._signer.did,
            classification_max=handoff.trace.classification,
        )
        subject = f"Handoff: {thread.subject}" if thread.subject else "Handoff"
        return await self._announce(
            to=tuple(item.participant_id for item in handoff.to_participants),
            subject=subject,
            body=(
                f"You have been handed mail thread {thread.conversation_id}"
                f" (handoff {handoff.handoff_id}). Read it in your inbox and take it over."
            ),
            idempotency_key=f"handoff:{handoff.handoff_id}",
            classification=handoff.trace.classification,
        )

    async def announce_handoff_resolution(self, handoff: Any) -> MailSendResult | None:
        """Tell the handoff's originator how it was resolved; nothing to tell oneself."""
        origin = handoff.from_participant.participant_id
        if origin == self._signer.did:
            return None
        status = str(getattr(handoff.status, "value", handoff.status))
        return await self._announce(
            to=(origin,),
            subject=f"Handoff {status}",
            body=f"Handoff {handoff.handoff_id} was {status}.",
            idempotency_key=f"handoff:{handoff.handoff_id}:{status}",
            classification=handoff.trace.classification,
        )

    async def _announce(
        self,
        *,
        to: tuple[str, ...],
        subject: str,
        body: str,
        idempotency_key: str,
        classification: str,
    ) -> MailSendResult:
        return await self.send(
            MailSendRequest(
                sender=await self._address_book.address_for(self._signer.did),
                sender_did=self._signer.did,
                to=to,
                subject=subject,
                body=body,
                idempotency_key=idempotency_key,
                classification=classification,
            )
        )

    async def create_handoff(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.create_handoff(*args, **kwargs)

    async def list_handoffs(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.list_handoffs(*args, **kwargs)

    async def resolve_handoff(self, *args: Any, **kwargs: Any) -> Any:
        return await self._store.resolve_handoff(*args, **kwargs)


class MailInboxDeliveryPort:
    """ArcStore ``InboxDeliveryPort`` whose effects travel as signed agent mail.

    The durable inbox is composed before the mail service that depends on it,
    so the port is built first and bound once the service exists. Unbound, it
    fails closed: the inbox reports the effect unavailable instead of pretending
    it was delivered.
    """

    def __init__(self) -> None:
        self._mail: AgentMailService | None = None

    def bind(self, mail: AgentMailService | None) -> None:
        """Attach (or detach, with ``None``) the mail service that carries effects."""
        self._mail = mail

    def _require(self) -> AgentMailService:
        if self._mail is None:
            raise RuntimeError("inbox delivery port is unavailable: agent mail is not composed")
        return self._mail

    async def deliver_reply(self, message: Any) -> None:
        """Mail replies travel through :meth:`AgentMailService.reply`, never this port."""
        del message
        raise RuntimeError("inbox delivery port is unavailable for replies: use agent mail")

    async def wake_handoff(self, handoff: Any) -> None:
        await self._require().announce_handoff(handoff)

    async def deliver_handoff_resolution(self, handoff: Any) -> None:
        await self._require().announce_handoff_resolution(handoff)


def _copy_owner(thread: Any, conversation_id: str, default: Any) -> Any:
    """Return the participant whose durable copy ``thread`` is (else ``default``)."""
    for item in thread.participants:
        if _thread_id_for(item.participant_id, conversation_id) == thread.thread_id:
            return item
    return default


def _thread_id_for(owner_id: str, conversation_id: str) -> str:
    from arcstore.inbox_projection import thread_id_for

    return thread_id_for(owner_id, conversation_id)


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


async def _transport_addresses(
    address_book: MailAddressBook, addresses: tuple[str, ...]
) -> tuple[str, ...]:
    """Render DID recipients as the ``agent://``/``user://`` addresses the bus routes."""
    rendered: list[str] = []
    for address in addresses:
        if address.startswith("did:"):
            rendered.append(await address_book.address_for(address))
        else:
            rendered.append(address)
    return tuple(rendered)


async def _resolve_addresses(
    address_book: MailAddressBook, participants: tuple[Any, ...]
) -> tuple[str, ...]:
    addresses: list[str] = []
    for participant in participants:
        addresses.append(await address_book.address_for(participant.participant_id))
    return tuple(addresses)


__all__ = [
    "MAIL_REPLY_LIMIT",
    "MAIL_TARGET_SCHEME",
    "AgentMailService",
    "MailAddressBook",
    "MailDeliveryWorker",
    "MailInboxDeliveryPort",
    "MailSendRequest",
    "MailSendResult",
    "MailThreadClosedError",
    "RegistryMailAddressBook",
    "conversation_of",
    "mail_participant",
    "mail_target",
]
