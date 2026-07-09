"""NATS JetStream storage backend.

``NatsBackend`` implements the :class:`arcteam.storage.StorageBackend` Protocol
over NATS JetStream so the messenger runs unchanged above it (REQ-022):

* **Records** (``read``/``write``/``delete``/``query``/``list_keys``/``exists``)
  map to a JetStream **KV bucket** per collection.
* **Streams** (``append``/``append_auto_seq``/``read_stream``/``read_last``)
  map to a JetStream **stream** per ``(collection, key)``. The JetStream
  publish sequence is the authoritative message ``seq`` — it is the ack floor
  that a durable consumer resumes from (REQ-021), replacing hand-rolled seq
  bookkeeping.
* **Durable consumers** (``open_consumer``) give each entity live push AND
  resume-from-last-ack on restart (REQ-021).

Subjects follow the REQ-020 families supplied by the messenger as stream keys
(``arc.agent.{name}``, ``arc.channel.{name}``, ``arc.role.{name}``). KV bucket
names and stream names must be single tokens, so they are hex-encoded; KV keys
and stream subjects preserve their readable form where NATS allows it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import KeyValueConfig
from nats.js.errors import (
    BucketNotFoundError,
    KeyNotFoundError,
    NoKeysError,
    NotFoundError,
)

from arcteam.storage import Delivery

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js import JetStreamContext
    from nats.js.kv import KeyValue

_logger = logging.getLogger("arcteam.backends.nats")

_FETCH_TIMEOUT = 1.0
# Bound the initial connect so an unreachable server fails fast instead of
# looping through nats-py's default reconnect budget (~2 min of 2 s retries).
_CONNECT_TIMEOUT = 3.0
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")


def _hex(text: str) -> str:
    """Encode arbitrary text to a NATS-safe single token (reversible)."""
    return text.encode("utf-8").hex()


def _unhex(token: str) -> str:
    return bytes.fromhex(token).decode("utf-8")


def _bucket_name(collection: str) -> str:
    return f"b{_hex(collection)}"


def _stream_name(collection: str, key: str) -> str:
    return "s" + _hex(collection + "\x00" + key)


def _subject(key: str) -> str:
    """Readable wire subject for a stream key, or a hex token if unsafe."""
    if _SUBJECT_RE.match(key):
        return key
    return f"k{_hex(key)}"


def _encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _decode(raw: bytes) -> dict[str, Any]:
    return json.loads(raw)  # type: ignore[no-any-return]  # reason: json.loads returns Any; records are contractually dict[str, Any]


class ConsumedMessage:
    """A message delivered by a durable consumer.

    ``ack`` advances the consumer's server-side ack floor so a restart of the
    same durable resumes past this message (REQ-021).
    """

    def __init__(self, data: dict[str, Any], raw: Any) -> None:
        self.data = data
        self._raw = raw

    async def ack(self) -> None:
        """Acknowledge the message, advancing the durable ack floor."""
        await self._raw.ack()


class NatsConsumer:
    """A durable pull consumer bound to one entity's stream."""

    def __init__(self, sub: Any) -> None:
        self._sub = sub

    async def fetch(self, batch: int) -> list[Delivery]:
        """Pull up to ``batch`` un-acked messages; empty list when idle."""
        try:
            msgs = await self._sub.fetch(batch, timeout=_FETCH_TIMEOUT)
        except NatsTimeout:
            return []
        deliveries: list[Delivery] = [ConsumedMessage(_decode(m.data), m) for m in msgs]
        return deliveries


class NatsBackend:
    """StorageBackend backed by NATS JetStream (records via KV, streams via JS)."""

    def __init__(self, js: JetStreamContext, nc: Client | None = None) -> None:
        self._js = js
        self._nc = nc

    @classmethod
    async def connect(
        cls, servers: str | list[str], *, connect_timeout: float = _CONNECT_TIMEOUT
    ) -> NatsBackend:
        """Open a JetStream-enabled NATS connection and wrap it.

        The initial connect is bounded by ``connect_timeout`` so an unreachable
        server raises ``asyncio.TimeoutError`` fast instead of blocking on
        nats-py's default reconnect budget. A quiet ``error_cb`` routes nats-py's
        async errors through our logger at debug, so a transient/connection error
        never reaches asyncio's default handler as a raw stderr traceback (F9).
        Reconnection after a successful connect stays enabled (nats-py default).
        """
        import nats

        async def _quiet_error(err: Exception) -> None:
            _logger.debug("NATS async error: %s", err)

        nc = await asyncio.wait_for(
            nats.connect(servers, error_cb=_quiet_error),
            timeout=connect_timeout,
        )
        return cls(nc.jetstream(), nc)

    async def close(self) -> None:
        """Drain and close the underlying connection, if this owns one."""
        if self._nc is not None:
            await self._nc.drain()

    # --- Records (KV) ---

    async def _open_kv(self, collection: str) -> KeyValue | None:
        try:
            return await self._js.key_value(_bucket_name(collection))
        except BucketNotFoundError:
            return None

    async def _ensure_kv(self, collection: str) -> KeyValue:
        bucket = _bucket_name(collection)
        try:
            return await self._js.key_value(bucket)
        except BucketNotFoundError:
            return await self._js.create_key_value(config=KeyValueConfig(bucket=bucket))

    async def read(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read a single JSON record."""
        kv = await self._open_kv(collection)
        if kv is None:
            return None
        try:
            entry = await kv.get(_hex(key))
        except KeyNotFoundError:
            return None
        if entry.value is None:
            return None
        return _decode(entry.value)

    async def write(self, collection: str, key: str, data: dict[str, Any]) -> None:
        """Write/overwrite a single JSON record."""
        kv = await self._ensure_kv(collection)
        await kv.put(_hex(key), _encode(data))

    async def delete(self, collection: str, key: str) -> bool:
        """Delete a record. Returns True if it existed."""
        kv = await self._open_kv(collection)
        if kv is None:
            return False
        try:
            await kv.get(_hex(key))
        except KeyNotFoundError:
            return False
        await kv.delete(_hex(key))
        return True

    async def exists(self, collection: str, key: str) -> bool:
        """Check if a record exists."""
        return await self.read(collection, key) is not None

    async def _all_keys(self, collection: str) -> list[str]:
        kv = await self._open_kv(collection)
        if kv is None:
            return []
        try:
            return await kv.keys()
        except NoKeysError:
            return []

    async def list_keys(self, collection: str, prefix: str | None = None) -> list[str]:
        """List all keys in a collection."""
        keys = sorted(_unhex(k) for k in await self._all_keys(collection))
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        return keys

    async def query(
        self,
        collection: str,
        filters: dict[str, Any] | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query records by field match or key prefix."""
        results: list[dict[str, Any]] = []
        for key in await self.list_keys(collection, prefix):
            data = await self.read(collection, key)
            if data is None:
                continue
            if filters and not all(data.get(k) == v for k, v in filters.items()):
                continue
            results.append(data)
        return results

    # --- Streams ---

    async def _ensure_stream(self, collection: str, key: str) -> str:
        name = _stream_name(collection, key)
        try:
            await self._js.stream_info(name)
        except NotFoundError:
            await self._js.add_stream(name=name, subjects=[_subject(key)])
        return name

    async def append(self, collection: str, key: str, entry: dict[str, Any]) -> int:
        """Append to a stream. Returns the assigned JetStream sequence."""
        seq, _ = await self.append_auto_seq(collection, key, entry)
        return seq

    async def append_auto_seq(
        self,
        collection: str,
        key: str,
        entry: dict[str, Any],
    ) -> tuple[int, int]:
        """Publish to the stream. Returns ``(jetstream_seq, 0)``.

        The JetStream publish sequence is the authoritative message ``seq`` and
        the ack floor a durable consumer resumes from; there is no separate
        byte offset (that was a file-backend seek hint).
        """
        name = await self._ensure_stream(collection, key)
        ack = await self._js.publish(_subject(key), _encode(entry), stream=name)
        return ack.seq, 0

    async def read_stream(
        self,
        collection: str,
        key: str,
        after_seq: int = 0,
        byte_pos: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read stream entries with ``seq > after_seq`` (seq-addressed)."""
        name = _stream_name(collection, key)
        try:
            info = await self._js.stream_info(name)
        except NotFoundError:
            return []
        last = info.state.last_seq
        results: list[dict[str, Any]] = []
        seq = after_seq + 1
        while seq <= last and len(results) < limit:
            try:
                msg = await self._js.get_msg(name, seq=seq)
            except NotFoundError:
                seq += 1
                continue
            if msg.data is not None:
                record = _decode(msg.data)
                record["seq"] = msg.seq
                results.append(record)
            seq += 1
        return results

    async def read_last(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read the last entry from a stream."""
        name = _stream_name(collection, key)
        try:
            msg = await self._js.get_last_msg(name, _subject(key))
        except NotFoundError:
            return None
        if msg.data is None:
            return None
        record = _decode(msg.data)
        record["seq"] = msg.seq
        return record

    async def get_stream_end_byte_pos(self, collection: str, key: str) -> int:
        """Byte offsets are unused on JetStream (seq-addressed); always 0."""
        return 0

    # --- Durable consumers (REQ-021) ---

    async def open_consumer(self, collection: str, key: str, durable: str) -> NatsConsumer:
        """Bind (or create) a durable pull consumer for ``(collection, key)``.

        Live delivery plus resume-from-last-ack: re-opening the same ``durable``
        after a restart continues from the server-owned ack floor.
        """
        name = await self._ensure_stream(collection, key)
        sub = await self._js.pull_subscribe(_subject(key), durable=durable, stream=name)
        return NatsConsumer(sub)
