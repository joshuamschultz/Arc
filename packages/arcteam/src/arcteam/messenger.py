"""MessagingService: pull-based messaging for autonomous agents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, NoReturn  # used for DLQ entry dicts

from arctrust.classification import Classification, dominates, parse_classification
from pydantic import ValidationError

from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner, ReplayCache, new_nonce, sign_message, verify_message
from arcteam.mentions import apply_mentions
from arcteam.registry import EntityRegistry, UnknownHandle, resolve_ref
from arcteam.storage import Consumer, Delivery, StorageBackend
from arcteam.types import (
    MAX_BODY_BYTES,
    Channel,
    Cursor,
    Entity,
    Message,
    generate_message_id,
    parse_uri,
)

logger = logging.getLogger(__name__)

STREAMS_COLLECTION = "messages/streams"
CHANNELS_COLLECTION = "messages/channels"
CURSORS_COLLECTION = "messages/cursors"
DLQ_COLLECTION = "dlq"
DLQ_KEY = "dlq"

# Consume-loop tuning. The push loop fetches a batch, dispatches each message,
# then re-fetches; the idle sleep bounds the busy-spin when a stream is quiet
# (the NATS consumer also blocks up to its own fetch timeout).
_FETCH_BATCH = 20
_IDLE_SLEEP = 0.05
# How often a live subscription re-resolves its channel membership. Short enough
# that adding an agent to a channel from the dashboard feels immediate; long
# enough that it is one registry read per entity per interval, not per message.
_RESUBSCRIBE_INTERVAL = 15.0

MessageHandler = Callable[["Message"], Awaitable[None]]


class RetryableDeliveryError(Exception):
    """Signal from a subscribe handler that delivery hit transient backpressure.

    The dispatcher responds by NOT acking the message, so the durable consumer
    redelivers it after ``ack_wait`` — once the downstream (e.g. a full agent
    steering queue) has drained. Use only for recoverable saturation; a
    permanent (poison) failure must NOT raise this, or it will redeliver forever.
    """


def _durable_name(entity_id: str, stream: str) -> str:
    """NATS-safe durable name unique per (entity, stream).

    A durable identifies the consumer whose ack floor a re-subscribe resumes
    from (REQ-021). NATS durable names cannot contain ``.``/``://``, so both
    parts are flattened to a single token.
    """
    safe_entity = entity_id.replace("://", "_").replace(".", "_")
    safe_stream = stream.replace(".", "_")
    return f"{safe_entity}__{safe_stream}"


def _stream_name_from_uri(uri: str) -> str:
    """Convert URI to NATS-compatible stream name.

    channel://ops -> arc.channel.ops
    role://procurement -> arc.role.procurement
    agent://a1 -> arc.agent.a1
    user://josh -> arc.agent.josh
    """
    scheme, name = parse_uri(uri)
    if scheme in ("agent", "user"):
        return f"arc.agent.{name}"
    return f"arc.{scheme}.{name}"


def _cursor_key(stream: str, entity_id: str) -> str:
    """Build cursor key: stream/entity_safe_id."""
    safe = entity_id.replace("://", "_")
    return f"{stream}/{safe}"


class Subscription:
    """Handle for a running push subscription; ``stop`` cancels its loops.

    The task set grows: a channel this entity joins after subscribing gets its
    own consume loop from the re-resolve supervisor, so ``stop`` must cancel
    what is present when it runs, not what was present at construction.
    """

    def __init__(self, base: str, handler: MessageHandler) -> None:
        self.base = base
        self.handler = handler
        # Shared across every consume loop here, so a message fanned to two of
        # this entity's streams is delivered once, not replayed.
        self.seen_ids: set[str] = set()
        self.streams: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """The consume loops currently running under this subscription."""
        return list(self._tasks)

    def track(self, task: asyncio.Task[None]) -> None:
        """Adopt a loop opened after the subscription started."""
        self._tasks.append(task)

    async def wait(self) -> None:
        """Block until every consume loop ends (they run until cancelled)."""
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        """Cancel every consume loop and wait for them to unwind."""
        tasks = self.tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class MessagingService:
    """Push + pull messaging. Zero arcagent dependency. Standalone service."""

    def __init__(
        self,
        backend: StorageBackend,
        registry: EntityRegistry,
        audit: AuditLogger,
        signer: MessageSigner | None = None,
        strict_classification: bool = False,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._audit = audit
        # SPEC-038 REQ-026 — federal fails closed on an unresolvable recipient
        # clearance or an unknown classification label. Personal/enterprise
        # default to permissive UNCLASSIFIED.
        self._strict_classification = strict_classification
        # The signer signs messages this service *sends* (REQ-030). It does NOT
        # gate verification: every consumed message is Ed25519-verified,
        # sender-bound, and replay/dedup-checked regardless of whether this
        # service can sign — a keyless receiver still rejects forged traffic.
        self._signer = signer
        self._replay = ReplayCache()
        self._dlq_seq: int | None = None
        self._known_streams: set[str] = set()
        self._cursor_cache: dict[str, Cursor] = {}

    async def _next_dlq_seq(self) -> int:
        """Get next DLQ sequence number (cached)."""
        if self._dlq_seq is None:
            last = await self._backend.read_last(DLQ_COLLECTION, DLQ_KEY)
            self._dlq_seq = last.get("seq", 0) if last else 0
        self._dlq_seq += 1
        return self._dlq_seq

    async def _to_dlq(self, message: Message, reason: str) -> None:
        """Send failed message to Dead Letter Queue."""
        await self._quarantine(message.model_dump(), reason)

    async def _quarantine(self, payload: dict[str, Any], reason: str) -> None:
        """Record one dead-letter entry, with its reason, for any payload (REQ-313).

        Takes a raw dict rather than a ``Message`` so a payload this consumer
        cannot even parse still leaves a record an operator can query — a
        message that stops travelling is never allowed to leave nothing behind.

        The payload and its ``meta`` are both copied: a caller may hand over the
        live stream record, and stamping the reason onto that record in place
        would rewrite the message still sitting on the stream. ``meta`` is
        rebuilt when it is missing or not a mapping — stream data is untrusted,
        and the reason has to survive whatever shape it arrives in.
        """
        entry = dict(payload)
        meta = entry.get("meta")
        entry["meta"] = dict(meta) if isinstance(meta, dict) else {}
        entry["meta"]["dlq_reason"] = reason
        entry["meta"]["dlq_timestamp"] = datetime.now(UTC).isoformat()
        entry["seq"] = await self._next_dlq_seq()
        await self._backend.append(DLQ_COLLECTION, DLQ_KEY, entry)

    # --- Snapshot-based entity lookup ---

    @staticmethod
    def _entity_by_ref(entities: list[Entity], ref: str) -> Entity | None:
        """Look up a full entity by any address ref against one snapshot.

        The synchronous, snapshot-backed counterpart to
        :meth:`EntityRegistry.get`: the caller fetches the entity list once per
        message and every clearance/signer lookup reads from it, so a single
        send resolves every ref with one backend query instead of one per ref.
        """
        try:
            did = resolve_ref(entities, ref)
        except UnknownHandle:
            return None
        if not did.startswith("did:"):
            return None
        for entity in entities:
            if entity.did == did:
                return entity
        return None

    # --- Channel membership check ---

    @staticmethod
    def _member_dids(channel: Channel, entities: list[Entity]) -> set[str]:
        """Resolve a channel's stored member refs to DIDs against one snapshot.

        The single membership-comparison primitive: every stored ref (handle,
        URI, or DID) collapses to a DID so membership matches regardless of the
        form a member joined under. Resolving all members against one entity
        snapshot keeps a membership check O(N+M), not O(N*M).
        """
        dids: set[str] = set()
        for member in channel.members:
            try:
                dids.add(resolve_ref(entities, member))
            except UnknownHandle:
                continue
        return dids

    async def _check_channel_membership(
        self, sender: str, channel_name: str, entities: list[Entity]
    ) -> bool:
        """Verify sender is a member of the target channel (FR-7).

        Both the sender and the stored members are compared by DID — an entity
        matches whether it was added by handle, URI, or DID. Resolves against
        the caller's per-send entity snapshot rather than re-querying.
        """
        data = await self._backend.read(CHANNELS_COLLECTION, channel_name)
        if data is None:
            return False
        channel = Channel.model_validate(data)
        try:
            sender_did = resolve_ref(entities, sender)
        except UnknownHandle:
            return False
        return sender_did in self._member_dids(channel, entities)

    # --- Classification (no-write-down) ---

    async def _resolve_recipient_clearance(
        self, scheme: str, name: str, entities: list[Entity]
    ) -> Classification | None:
        """Resolve a recipient/channel/role clearance, or None if unresolvable.

        Role targets fan out to members; the strictest (lowest) member clearance
        governs — a role message may only go as low as its least-cleared member.
        Entity/role clearances read from the caller's per-send snapshot; only a
        channel clearance touches the (non-registry) channel store.
        """
        if scheme in ("agent", "user"):
            entity = self._entity_by_ref(entities, f"{scheme}://{name}")
            if entity is None:
                return None
            return parse_classification(entity.clearance, strict=self._strict_classification)
        if scheme == "channel":
            data = await self._backend.read(CHANNELS_COLLECTION, name)
            if data is None:
                return None
            channel = Channel.model_validate(data)
            return parse_classification(channel.clearance, strict=self._strict_classification)
        if scheme == "role":
            members = [e for e in entities if name in e.roles]
            if not members:
                return None
            levels = [
                parse_classification(m.clearance, strict=self._strict_classification)
                for m in members
            ]
            return min(levels)
        return None

    async def _enforce_no_write_down(
        self, message: Message, scheme: str, name: str, uri: str, entities: list[Entity]
    ) -> None:
        """Refuse a send whose classification the recipient cannot receive.

        Fail closed: an unresolvable recipient clearance (or an unknown label at
        federal) refuses delivery (REQ-024/026). NIST 800-53 AC-4.
        """
        msg_level = parse_classification(
            message.classification, strict=self._strict_classification
        )
        recipient_level = await self._resolve_recipient_clearance(scheme, name, entities)
        if recipient_level is None:
            if not self._strict_classification:
                recipient_level = Classification.UNCLASSIFIED
            else:
                await self._refuse_classification(message, uri, "unresolvable recipient clearance")

        if recipient_level is not None and dominates(recipient_level, msg_level):
            return
        await self._refuse_classification(
            message,
            uri,
            f"message classification {msg_level.name} exceeds recipient clearance",
        )

    async def _refuse_classification(self, message: Message, uri: str, detail: str) -> NoReturn:
        """Audit + DLQ a classification refusal, then raise (fail closed)."""
        await self._to_dlq(message, "classification_refused")
        await self._audit.log(
            event_type="message.classification_refused",
            subject=uri,
            actor_id=message.sender,
            detail=f"{detail} (to {uri})",
            classification=message.classification,
        )
        raise ValueError(f"Message refused: {detail} for {uri}")

    # --- Send ---

    async def send(self, message: Message) -> Message:
        """Send a message. Routes to appropriate stream(s) based on `to` URIs.

        Auto-assigns seq, id, ts, thread_id. Validates body size.
        Enforces channel membership for channel:// targets (FR-7).
        """
        # One registry snapshot serves every resolve in this message — sender,
        # @-targets, body mentions, and recipient/role clearance. Resolving each
        # ref against its own backend query would be O(N*M) reads per send.
        entities = await self._registry.list_entities()

        # Validate sender is a registered entity. An unknown sender raises
        # UnknownHandle (REQ-002) — never a silent sender_unauthorized DLQ.
        sender_did = resolve_ref(entities, message.sender)
        if not sender_did.startswith("did:"):
            raise UnknownHandle(f"Sender is not a registered entity: {message.sender}")

        # Validate body size
        if len(message.body.encode("utf-8")) > MAX_BODY_BYTES:
            await self._to_dlq(message, "body_too_large")
            raise ValueError("Message body exceeds 64KB limit")

        # Auto-assign fields
        now = datetime.now(UTC).isoformat()
        message.id = generate_message_id()
        message.ts = now

        # Threading: caller sets thread_id for replies; new messages self-reference.
        if not message.thread_id:
            message.thread_id = message.id

        # Record @mentions from the body and raise attention flags (REQ-004).
        apply_mentions(entities, message)

        # Sign the finalized envelope (REQ-030). The signature covers the body
        # and mentions, so it must run after they are set and before routing.
        if self._signer is not None:
            message.signer_did = self._signer.did
            message.nonce = new_nonce()
            sign_message(message, self._signer.private_key)

        # Serialize once before the loop (not per-target)
        base_dict = message.model_dump()

        # Route to each target
        streams_written: list[str] = []
        channel_names: list[str] = []
        last_seq = 0
        for target in message.to:
            # `@handle` is sugar for addressing an entity's inbox. Resolve it
            # (raising UnknownHandle for an unknown handle, e.g. @ghost) and
            # normalize to the agent URI so routing stays name-based.
            if target.startswith("@"):
                resolve_ref(entities, target)
                uri = f"agent://{target[1:]}"
            else:
                uri = target
            try:
                scheme, name = parse_uri(uri)
                stream = _stream_name_from_uri(uri)
            except ValueError:
                await self._to_dlq(message, "invalid_address")
                raise

            # REQ-313: an entity target must resolve to a registered entity, in
            # whichever form it was addressed. `@ghost` already raises here via
            # resolve_ref; without this the `agent://ghost` form was accepted,
            # written to a stream nothing subscribes to, and reported "sent" —
            # neither delivered nor recorded anywhere as failed.
            if scheme in ("agent", "user") and self._entity_by_ref(entities, uri) is None:
                raise UnknownHandle(f"Addressee is not a registered entity: {uri}")

            # FR-7: Enforce channel membership
            if scheme == "channel":
                channel_names.append(name)
                is_member = await self._check_channel_membership(message.sender, name, entities)
                if not is_member:
                    await self._to_dlq(message, "not_channel_member")
                    raise ValueError(
                        f"Sender {message.sender} is not a member of channel://{name}"
                    )

            # SPEC-038 REQ-024 — no-write-down: the recipient/channel clearance
            # must dominate the message classification, or the send is refused.
            await self._enforce_no_write_down(message, scheme, name, uri, entities)

            msg_dict = {**base_dict}
            seq, _offset = await self._backend.append_auto_seq(
                STREAMS_COLLECTION,
                stream,
                msg_dict,
            )
            self._known_streams.add(stream)
            streams_written.append(stream)
            last_seq = seq

            await self._audit.log(
                event_type="message.sent",
                subject=f"{stream}",
                actor_id=message.sender,
                detail=f"Message {message.id} to {uri} (seq={seq})",
                stream=stream,
                msg_seq=seq,
            )

        # A channel @mention must reliably WAKE the mentioned entity even if it is
        # not a channel member (or subscribed to the channel stream yet), so fan
        # the same envelope into each mentioned entity's inbox.
        if channel_names and message.mentions:
            await self._fanout_mentions_to_inboxes(
                message, entities, base_dict, streams_written, sender_did, channel_names
            )

        message.seq = last_seq
        message.status = "sent"
        return message

    async def _fanout_mentions_to_inboxes(
        self,
        message: Message,
        entities: list[Entity],
        base_dict: dict[str, Any],
        streams_written: list[str],
        sender_did: str,
        channel_names: list[str],
    ) -> None:
        """Deliver a channel @mention to each mentioned entity's inbox (REQ-004).

        Guarantees the mentioned entity is woken regardless of channel membership
        or subscribe timing — its inbox (``arc.agent.{handle}``) is always
        subscribed. A member who is ALSO mentioned already received the message on
        the channel stream; the inbox copy is a benign duplicate the receiver's
        ``subscribe`` dedups by message id (shared ``seen_ids``), so it is never
        run twice. The sender is never self-notified, and an under-cleared mention
        is skipped (no-write-down, AC-4) rather than refusing the whole send.

        The same @mention also pulls the agent INTO each channel it was named in
        and is cleared for. The wake path already reaches a mentioned non-member;
        without membership its reply is later refused as ``not_channel_member`` and
        the answer is lost. Joining keeps wake and reply symmetric — bounded by the
        channel's own clearance, so a mention never lifts an agent into a room it
        may not read.
        """
        msg_level = parse_classification(
            message.classification, strict=self._strict_classification
        )
        channel_levels = await self._channel_levels(channel_names)
        for did in message.mentions:
            if did == sender_did:
                continue
            entity = next((e for e in entities if e.did == did), None)
            if entity is None:
                continue
            recipient_level = parse_classification(
                entity.clearance, strict=self._strict_classification
            )
            stream = f"arc.agent.{entity.handle}"
            # Inbox fan-out: one copy unless already delivered on a stream, never
            # below the message's own classification (no-write-down).
            if stream not in streams_written and dominates(recipient_level, msg_level):
                seq, _offset = await self._backend.append_auto_seq(
                    STREAMS_COLLECTION, stream, {**base_dict}
                )
                self._known_streams.add(stream)
                await self._audit.log(
                    event_type="message.mention_fanout",
                    subject=stream,
                    actor_id=message.sender,
                    detail=f"@mention fanned to {entity.handle} inbox (msg {message.id})",
                    stream=stream,
                    msg_seq=seq,
                )
            # Join-on-mention: findable to reply, only where cleared to read.
            for channel_name in channel_names:
                if dominates(recipient_level, channel_levels[channel_name]):
                    await self.join_channel(channel_name, did)

    async def _channel_levels(self, channel_names: list[str]) -> dict[str, Any]:
        """The parsed clearance of each targeted channel, for membership gating."""
        levels: dict[str, Any] = {}
        for name in channel_names:
            data = await self._backend.read(CHANNELS_COLLECTION, name)
            channel = Channel.model_validate(data) if data else Channel(name=name)
            levels[name] = parse_classification(
                channel.clearance, strict=self._strict_classification
            )
        return levels

    # --- Poll ---

    async def poll(
        self,
        stream: str,
        entity_id: str,
        max_messages: int = 10,
    ) -> list[Message]:
        """Pull unread messages from a stream for this entity.

        Reads from cursor position forward. Does NOT advance cursor.
        """
        cursor = await self.get_cursor(stream, entity_id)
        after_seq = cursor.seq if cursor else 0
        byte_pos = cursor.byte_pos if cursor else 0

        records = await self._backend.read_stream(
            STREAMS_COLLECTION,
            stream,
            after_seq=after_seq,
            byte_pos=byte_pos,
            limit=max_messages,
        )
        return [Message.model_validate(r) for r in records]

    async def poll_all(
        self,
        entity_id: str,
        max_per_stream: int = 10,
    ) -> dict[str, list[Message]]:
        """Poll all subscribed streams (inbox + channels + roles).

        Returns {stream_name: [messages]}. Uses asyncio.gather for parallelism.
        """
        entity = await self._registry.get(entity_id)
        if entity is None:
            return {}

        streams = self.resolve_subscriptions(entity_id, entity.roles)
        # Also include channels the entity is a member of. Membership is decided
        # by DID (via _member_dids) exactly as the send-side check, so an entity
        # that joined under one address form still matches when polling by another.
        channels = await self.list_channels()
        if channels:
            entities = await self._registry.list_entities()
            for ch in channels:
                if entity.did in self._member_dids(ch, entities):
                    stream = f"arc.channel.{ch.name}"
                    if stream not in streams:
                        streams.append(stream)

        # Parallel poll all streams
        async def _poll_one(stream: str) -> tuple[str, list[Message]]:
            msgs = await self.poll(stream, entity_id, max_per_stream)
            return stream, msgs

        tasks = [_poll_one(s) for s in streams]
        poll_results = await asyncio.gather(*tasks)

        return {stream: msgs for stream, msgs in poll_results if msgs}

    # --- Consume (verify + replay) ---

    async def receive(
        self,
        stream: str,
        entity_id: str,
        max_messages: int = 10,
    ) -> list[Message]:
        """Consume unread messages, verifying each before delivery.

        Verification is unconditional — it does not depend on whether this
        service can itself sign. Every message is Ed25519-verified against its
        signer's public key, bound to its declared sender, and replay-checked
        against the nonce window; any failure quarantines it to the DLQ
        (``bad_signature`` / ``replay``) and drops it from the returned batch
        (REQ-030, REQ-031). An unsigned or forged message is never delivered.
        """
        candidates = await self.poll(stream, entity_id, max_messages)
        delivered: list[Message] = []
        for message in candidates:
            reason = await self._verify_origin(message)
            if reason is not None:
                await self._to_dlq(message, reason)
                continue
            if not self._replay.check_and_record(message.nonce, message.ts):
                await self._to_dlq(message, "replay")
                continue
            delivered.append(message)
        return delivered

    async def _verify_origin(self, message: Message) -> str | None:
        """Return ``"bad_signature"`` if signature or sender-binding fails, else None.

        Two independent checks, both required (REQ-030):

        * The signature must verify against the *signer's* registered public
          key — an unregistered ``signer_did`` or one lacking a key is rejected.
        * The declared ``sender`` must resolve to the same DID as ``signer_did``,
          so a registered peer cannot publish ``sender=agent://alice`` under its
          own key and have it accepted as from alice (origin forgery).

        Replay/dedup is the caller's concern (``receive`` uses the nonce window;
        ``subscribe`` dedups by message id), so this method is state-free and
        safe to run once per delivery on either path.
        """
        entities = await self._registry.list_entities()
        signer = self._entity_by_ref(entities, message.signer_did)
        if signer is None or not signer.public_key:
            return "bad_signature"
        if not verify_message(message, bytes.fromhex(signer.public_key)):
            return "bad_signature"
        try:
            if resolve_ref(entities, message.sender) != message.signer_did:
                return "bad_signature"
        except UnknownHandle:
            return "bad_signature"
        return None

    # --- Subscribe (durable PUSH) ---

    async def subscribe(
        self,
        entity_id: str,
        handler: MessageHandler,
        *,
        durable_name: str | None = None,
    ) -> Subscription:
        """Push live messages to ``handler`` over durable consumers (REQ-021).

        Opens a durable pull consumer per subscribed stream (inbox, roles, and
        member channels) and runs a background fetch+dispatch loop for each —
        the modern push model: a running entity is delivered to live AND a
        restarted entity resumes from its last ack. Every delivery is verified
        (signature + sender-binding) exactly as :meth:`receive`; invalid ones go
        to the DLQ. A single message fanned out to two of this entity's streams
        (e.g. its inbox and a matching ``role://``) is delivered exactly once —
        the second copy is a benign duplicate, acked and skipped, never a
        ``replay``. Valid, first-seen messages reach ``await handler(message)``
        before being acked. Returns a :class:`Subscription` whose ``stop``
        cancels the loops.
        """
        subscription = Subscription(durable_name or entity_id, handler)
        await self.refresh_subscription(entity_id, subscription)
        subscription.track(
            asyncio.create_task(
                self._resubscribe_loop(entity_id, subscription),
                name=f"arcteam-resubscribe:{subscription.base}",
            )
        )
        return subscription

    async def _subscription_streams(self, entity_id: str) -> list[str]:
        """Every stream ``entity_id`` should consume right now.

        Its inbox, one per role, and one per channel it is currently a member
        of. Resolved on demand rather than cached: channel membership changes
        while an agent runs (an operator adds it from the dashboard), and a
        boot-time snapshot is how a member of a channel receives nothing from it
        until the process restarts.
        """
        entity = await self._registry.get(entity_id)
        roles = entity.roles if entity is not None else []
        streams = self.resolve_subscriptions(entity_id, roles)
        if entity is None:
            return streams
        channels = await self.list_channels()
        if not channels:
            return streams
        entities = await self._registry.list_entities()
        for channel in channels:
            if entity.did in self._member_dids(channel, entities):
                stream = f"arc.channel.{channel.name}"
                if stream not in streams:
                    streams.append(stream)
        return streams

    async def _open_stream(self, stream: str, subscription: Subscription) -> None:
        """Open one durable consumer and attach its consume loop, once."""
        if stream in subscription.streams:
            return
        durable = _durable_name(subscription.base, stream)
        consumer = await self._backend.open_consumer(STREAMS_COLLECTION, stream, durable)
        self._known_streams.add(stream)
        subscription.streams.add(stream)
        subscription.track(
            asyncio.create_task(
                self._consume_stream(consumer, subscription.handler, subscription.seen_ids),
                name=f"arcteam-subscribe:{durable}",
            )
        )

    async def refresh_subscription(self, entity_id: str, subscription: Subscription) -> None:
        """Open consumers for streams ``entity_id`` joined since it subscribed.

        Idempotent: a stream already being consumed is skipped, so calling this
        repeatedly never opens a second consumer or double-delivers. Public
        because the supervisor's interval is too coarse for a test to wait on,
        and because a surface that just changed membership can apply it now.
        """
        for stream in await self._subscription_streams(entity_id):
            await self._open_stream(stream, subscription)

    async def _resubscribe_loop(self, entity_id: str, subscription: Subscription) -> None:
        """Re-resolve membership on a timer for the life of the subscription.

        Fail-open and never fatal: a registry or broker hiccup logs and retries
        on the next tick rather than killing the supervisor, because losing it
        silently would restore exactly the boot-snapshot behaviour this exists
        to remove.
        """
        while True:
            await asyncio.sleep(_RESUBSCRIBE_INTERVAL)
            try:
                await self.refresh_subscription(entity_id, subscription)
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: fail-open — retry on the next tick
                logger.exception("resubscribe: membership refresh failed; retrying")

    async def _consume_stream(
        self, consumer: Consumer, handler: MessageHandler, seen_ids: set[str]
    ) -> None:
        """Fetch-dispatch loop for one durable consumer until cancelled."""
        while True:
            try:
                deliveries = await consumer.fetch(_FETCH_BATCH)
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: fail-open — log + retry after a beat
                logger.exception("consume fetch failed; retrying")
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            if not deliveries:
                await asyncio.sleep(_IDLE_SLEEP)
                continue
            for delivery in deliveries:
                await self._dispatch(delivery, handler, seen_ids)

    async def _dispatch(
        self, delivery: Delivery, handler: MessageHandler, seen_ids: set[str]
    ) -> None:
        """Verify one delivery, hand valid first-seen ones to the handler, then ack.

        Verification is unconditional (signature + sender-binding); invalid
        messages are quarantined to the DLQ and still acked so they are not
        redelivered. A message id already delivered on this subscription is a
        benign fan-out duplicate — acked and skipped, never delivered twice. A
        handler that raises :class:`RetryableDeliveryError` (transient
        backpressure) is NOT acked, so it redelivers rather than being lost; any
        other handler failure is dead-lettered with a reason and acked, so a
        poison message neither loops forever nor vanishes without a record
        (REQ-313).
        """
        try:
            message = Message.model_validate(delivery.data)
        except ValidationError:
            # Untrusted stream data — a malformed payload must not escape the
            # consume loop and kill the unsupervised task. Quarantine it with a
            # reason and ack: dropping the poison message is right, dropping it
            # with only a log line behind it is the silent drop REQ-313 forbids.
            logger.exception("dead-lettering unparseable delivery (poison message)")
            await self._quarantine(delivery.data, "unparseable_envelope")
            await delivery.ack()
            return
        reason = await self._verify_origin(message)
        if reason is not None:
            await self._to_dlq(message, reason)
            await delivery.ack()
            return
        if message.id in seen_ids:
            await delivery.ack()
            return
        # Claim the id BEFORE awaiting the handler (REQ-314). The check and the
        # record have to be one synchronous step: an addressee coming back to a
        # backlog has the same message waiting on two of its streams at once, so
        # both consume loops reach here in the same tick and — with the record
        # on the far side of the await — both would deliver it. A duplicated
        # instruction to an agent is a duplicated action, not a cosmetic repeat.
        seen_ids.add(message.id)
        try:
            await handler(message)
        except asyncio.CancelledError:
            seen_ids.discard(message.id)
            raise
        except RetryableDeliveryError:
            # Transient downstream backpressure — release the claim and do NOT
            # ack; the durable consumer redelivers after ack_wait so the message
            # is deferred rather than lost.
            seen_ids.discard(message.id)
            logger.warning("inbox delivery deferred (backpressure) for %s", message.id)
            return
        except Exception:  # reason: permanent failure — record it, then ack
            logger.exception("inbox handler failed for message %s", message.id)
            await self._to_dlq(message, "handler_failed")
        await delivery.ack()

    # --- Cursor ---

    async def ack(
        self,
        stream: str,
        entity_id: str,
        seq: int,
        byte_pos: int,
    ) -> None:
        """Advance cursor after successful processing. Forward-only."""
        current = await self.get_cursor(stream, entity_id)
        if current and seq <= current.seq:
            logger.warning(
                "Rejected backward cursor advance for %s on %s: %d <= %d",
                entity_id,
                stream,
                seq,
                current.seq,
            )
            return

        cursor = Cursor(
            consumer=entity_id,
            stream=stream,
            seq=seq,
            byte_pos=byte_pos,
            updated_at=datetime.now(UTC).isoformat(),
        )
        cursor_key = _cursor_key(stream, entity_id)
        await self._backend.write(CURSORS_COLLECTION, cursor_key, cursor.model_dump())
        # Update cache
        self._cursor_cache[cursor_key] = cursor

    async def get_cursor(self, stream: str, entity_id: str) -> Cursor | None:
        """Get current cursor position (cached)."""
        cursor_key = _cursor_key(stream, entity_id)
        if cursor_key in self._cursor_cache:
            return self._cursor_cache[cursor_key]
        data = await self._backend.read(CURSORS_COLLECTION, cursor_key)
        if data is None:
            return None
        cursor = Cursor.model_validate(data)
        self._cursor_cache[cursor_key] = cursor
        return cursor

    # --- Stale Cursor Cleanup (FR-11) ---

    async def cleanup_stale_cursors(self, max_age_hours: int = 24) -> int:
        """Remove cursors older than max_age_hours. Returns count of removed cursors."""
        cutoff = datetime.now(UTC)
        keys = await self._backend.list_keys(CURSORS_COLLECTION)
        removed = 0

        for key in keys:
            data = await self._backend.read(CURSORS_COLLECTION, key)
            if data is None:
                continue
            updated_at = data.get("updated_at", "")
            if not updated_at:
                continue
            try:
                cursor_time = datetime.fromisoformat(updated_at)
                age_hours = (cutoff - cursor_time).total_seconds() / 3600
                if age_hours > max_age_hours:
                    await self._backend.delete(CURSORS_COLLECTION, key)
                    self._cursor_cache.pop(key, None)
                    removed += 1
            except (ValueError, TypeError):
                continue

        if removed > 0:
            await self._audit.log(
                event_type="cursor.cleanup",
                subject="cursors",
                actor_id="system",
                detail=f"Removed {removed} stale cursors (older than {max_age_hours}h)",
            )
        return removed

    # --- Channels ---

    async def create_channel(self, channel: Channel) -> None:
        """Create a channel and its stream directory.

        Raises:
            ValueError: If a channel with this name already exists — a
                blind write here would silently overwrite the existing
                channel's membership (every caller previously had to build
                its own list_channels() pre-check to avoid this).
        """
        existing = await self._backend.read(CHANNELS_COLLECTION, channel.name)
        if existing is not None:
            raise ValueError(f"Channel already exists: {channel.name}")
        if not channel.created:
            channel.created = datetime.now(UTC).isoformat()
        await self._backend.write(CHANNELS_COLLECTION, channel.name, channel.model_dump())
        await self._audit.log(
            event_type="channel.created",
            subject=f"arc.channel.{channel.name}",
            actor_id="system",
            detail=f"Channel '{channel.name}' created with members {channel.members}",
        )

    async def join_channel(self, channel_name: str, entity_id: str) -> None:
        """Add entity to channel membership."""
        data = await self._backend.read(CHANNELS_COLLECTION, channel_name)
        if data is None:
            raise ValueError(f"Channel not found: {channel_name}")
        channel = Channel.model_validate(data)
        if entity_id not in channel.members:
            channel.members.append(entity_id)
            await self._backend.write(CHANNELS_COLLECTION, channel_name, channel.model_dump())
            await self._audit.log(
                event_type="channel.joined",
                subject=f"arc.channel.{channel_name}",
                actor_id=entity_id,
                detail=f"Joined channel '{channel_name}'",
            )

    async def leave_channel(self, channel_name: str, entity_id: str) -> None:
        """Remove entity from channel membership."""
        data = await self._backend.read(CHANNELS_COLLECTION, channel_name)
        if data is None:
            raise ValueError(f"Channel not found: {channel_name}")
        channel = Channel.model_validate(data)
        if entity_id in channel.members:
            channel.members.remove(entity_id)
            await self._backend.write(CHANNELS_COLLECTION, channel_name, channel.model_dump())
            await self._audit.log(
                event_type="channel.left",
                subject=f"arc.channel.{channel_name}",
                actor_id=entity_id,
                detail=f"Left channel '{channel_name}'",
            )

    async def list_channels(self) -> list[Channel]:
        """Query channel definitions."""
        records = await self._backend.query(CHANNELS_COLLECTION)
        return [Channel.model_validate(r) for r in records]

    async def list_channel_messages(
        self,
        channel_name: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[Message]:
        """Read messages on a channel chronologically.

        Wraps ``backend.read_stream`` against the channel's stream
        (``arc.channel.{name}``). Used by observability surfaces (the
        ArcUI Team Chat tab, CLI tools) that want a flat
        oldest-to-newest view without tracking per-consumer cursors —
        unlike ``poll``, this never advances any cursor.
        """
        stream = f"arc.channel.{channel_name}"
        records = await self._backend.read_stream(
            STREAMS_COLLECTION,
            stream,
            after_seq=after_seq,
            limit=limit,
        )
        return [Message.model_validate(r) for r in records]

    # --- Threads ---

    async def get_thread(self, stream: str, thread_id: str) -> list[Message]:
        """All messages in a thread, chronologically.

        Filters raw dicts before Pydantic validation for efficiency.
        """
        records = await self._backend.read_stream(
            STREAMS_COLLECTION, stream, after_seq=0, limit=100000
        )
        # Filter raw dicts BEFORE creating Pydantic objects
        thread_records = [r for r in records if r.get("thread_id") == thread_id]
        thread_msgs = [Message.model_validate(r) for r in thread_records]
        return sorted(thread_msgs, key=lambda m: m.seq)

    # --- DLQ ---

    async def dlq_list(self, limit: int = 50) -> list[dict[str, Any]]:
        """List Dead Letter Queue entries."""
        return await self._backend.read_stream(DLQ_COLLECTION, DLQ_KEY, after_seq=0, limit=limit)

    # --- Subscriptions ---

    def resolve_subscriptions(self, entity_id: str, roles: list[str] | None = None) -> list[str]:
        """Resolve all streams an entity should poll.

        Accepts the entity in any address form — ``@handle``, ``agent://handle``,
        ``user://handle``, or a bare handle — and normalizes to the same
        handle-based inbox stream ``send`` routes to (``arc.agent.{handle}``),
        plus one ``arc.role.{role}`` per role. Normalizing the ``@handle`` form
        here is essential: otherwise a poll for ``@builder`` would listen on
        ``arc.agent.@builder`` while ``send`` wrote to ``arc.agent.builder``.
        """
        if entity_id.startswith("@"):
            name = entity_id[1:]
        else:
            try:
                _, name = parse_uri(entity_id)
            except ValueError:
                name = entity_id

        streams = [f"arc.agent.{name}"]
        for role in roles or []:
            streams.append(f"arc.role.{role}")
        return streams
