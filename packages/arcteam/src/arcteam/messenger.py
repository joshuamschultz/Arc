"""MessagingService: pull-based messaging for autonomous agents."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import StorageBackend
from arcteam.types import (
    MAX_BODY_BYTES,
    Channel,
    Cursor,
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


class MessagingService:
    """Pull-based messaging. Zero arcagent dependency. Standalone service."""

    def __init__(
        self,
        backend: StorageBackend,
        registry: EntityRegistry,
        audit: AuditLogger,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._audit = audit
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
        entry = message.model_dump()
        entry["meta"]["dlq_reason"] = reason
        entry["meta"]["dlq_timestamp"] = datetime.now(UTC).isoformat()
        entry["seq"] = await self._next_dlq_seq()
        await self._backend.append(DLQ_COLLECTION, DLQ_KEY, entry)

    # --- Channel membership check ---

    async def _check_channel_membership(self, sender: str, channel_name: str) -> bool:
        """Verify sender is a member of the target channel (FR-7)."""
        data = await self._backend.read(CHANNELS_COLLECTION, channel_name)
        if data is None:
            return False
        channel = Channel.model_validate(data)
        return sender in channel.members

    # --- Send ---

    async def send(self, message: Message) -> Message:
        """Send a message. Routes to appropriate stream(s) based on `to` URIs.

        Auto-assigns seq, id, ts, thread_id. Validates body size.
        Enforces channel membership for channel:// targets (FR-7).
        """
        # Validate sender is registered
        sender = await self._registry.get(message.sender)
        if sender is None:
            await self._to_dlq(message, "sender_unauthorized")
            raise ValueError(f"Sender not registered: {message.sender}")

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

        # Serialize once before the loop (not per-target)
        base_dict = message.model_dump()

        # Route to each target
        streams_written: list[str] = []
        last_seq = 0
        for uri in message.to:
            try:
                scheme, name = parse_uri(uri)
                stream = _stream_name_from_uri(uri)
            except ValueError:
                await self._to_dlq(message, "invalid_address")
                raise

            # FR-7: Enforce channel membership
            if scheme == "channel":
                is_member = await self._check_channel_membership(message.sender, name)
                if not is_member:
                    await self._to_dlq(message, "not_channel_member")
                    raise ValueError(
                        f"Sender {message.sender} is not a member of channel://{name}"
                    )

            msg_dict = {**base_dict}
            seq, _offset = await self._backend.append_auto_seq(
                STREAMS_COLLECTION, stream, msg_dict,
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

        message.seq = last_seq
        message.status = "sent"
        return message

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
        # Also include channels the entity is a member of
        channels = await self.list_channels()
        for ch in channels:
            if entity_id in ch.members:
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
        """Create a channel and its stream directory."""
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

        - arc.agent.{name} (DM inbox, always)
        - arc.role.{role} (for each role)
        """
        try:
            _, name = parse_uri(entity_id)
        except ValueError:
            name = entity_id

        streams = [f"arc.agent.{name}"]
        for role in roles or []:
            streams.append(f"arc.role.{role}")
        return streams
