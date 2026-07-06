"""Storage backend protocol and the in-memory test backend.

The production substrate is :class:`arcteam.backends.nats.NatsBackend` (NATS
JetStream). ``MemoryBackend`` is the dependency-free backend for tests.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Delivery(Protocol):
    """One message handed to a durable consumer.

    ``ack`` advances the consumer's ack floor so a restart resumes past this
    message (REQ-021).
    """

    data: dict[str, Any]

    async def ack(self) -> None:
        """Acknowledge the message, advancing the durable ack floor."""
        ...


@runtime_checkable
class Consumer(Protocol):
    """A durable consumer bound to one stream: fetch a batch, ack each."""

    async def fetch(self, batch: int) -> list[Delivery]:
        """Pull up to ``batch`` un-acked messages; empty list when idle."""
        ...


@runtime_checkable
class StorageBackend(Protocol):
    """Swappable storage abstraction shared by the messenger, registry, and audit."""

    async def read(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read a single JSON record."""
        ...

    async def write(self, collection: str, key: str, data: dict[str, Any]) -> None:
        """Write/overwrite a single JSON record."""
        ...

    async def delete(self, collection: str, key: str) -> bool:
        """Delete a record. Returns True if it existed."""
        ...

    async def append(self, collection: str, key: str, entry: dict[str, Any]) -> int:
        """Append to a stream. Returns an opaque sequence/offset."""
        ...

    async def read_stream(
        self,
        collection: str,
        key: str,
        after_seq: int = 0,
        byte_pos: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read entries from a stream starting after ``after_seq``."""
        ...

    async def read_last(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read the last entry from a stream."""
        ...

    async def query(
        self,
        collection: str,
        filters: dict[str, Any] | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query records by field match or key prefix."""
        ...

    async def list_keys(self, collection: str, prefix: str | None = None) -> list[str]:
        """List all keys in a collection."""
        ...

    async def append_auto_seq(
        self,
        collection: str,
        key: str,
        entry: dict[str, Any],
    ) -> tuple[int, int]:
        """Atomically assign a sequence and append. Returns ``(seq, offset)``."""
        ...

    async def get_stream_end_byte_pos(self, collection: str, key: str) -> int:
        """Return the current end-of-stream byte offset (0 for seq-addressed backends)."""
        ...

    async def exists(self, collection: str, key: str) -> bool:
        """Check if a record exists."""
        ...

    async def open_consumer(self, collection: str, key: str, durable: str) -> Consumer:
        """Open (or rebind) a durable consumer for a stream (REQ-021).

        Re-opening the same ``durable`` resumes from the last-acked message.
        """
        ...


class MemoryDelivery:
    """A message pulled from ``MemoryBackend``; ``ack`` advances the durable floor."""

    def __init__(
        self, floors: dict[tuple[str, str, str], int], floor_key: tuple[str, str, str],
        data: dict[str, Any], seq: int,
    ) -> None:
        self._floors = floors
        self._floor_key = floor_key
        self.data = data
        self._seq = seq

    async def ack(self) -> None:
        """Advance the durable floor to this message's sequence."""
        self._floors[self._floor_key] = max(self._floors.get(self._floor_key, 0), self._seq)


class MemoryConsumer:
    """In-memory durable pull consumer over ``MemoryBackend`` streams.

    Mirrors JetStream semantics: ``fetch`` returns messages past the ack floor,
    which advances only on ``ack``, so re-binding the same ``durable`` resumes
    with no missed or duplicated messages.
    """

    def __init__(
        self,
        streams: dict[str, dict[str, list[dict[str, Any]]]],
        floors: dict[tuple[str, str, str], int],
        collection: str,
        key: str,
        durable: str,
    ) -> None:
        self._streams = streams
        self._floors = floors
        self._collection = collection
        self._key = key
        self._floor_key = (collection, key, durable)

    async def fetch(self, batch: int) -> list[Delivery]:
        """Return up to ``batch`` messages with ``seq`` past the ack floor."""
        floor = self._floors.get(self._floor_key, 0)
        entries = self._streams.get(self._collection, {}).get(self._key, [])
        pending = [e for e in entries if e.get("seq", 0) > floor][:batch]
        deliveries: list[Delivery] = [
            MemoryDelivery(self._floors, self._floor_key, dict(entry), entry.get("seq", 0))
            for entry in pending
        ]
        return deliveries


class MemoryBackend:
    """In-memory storage backend for unit tests. Dict-backed, no filesystem."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._streams: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._stream_bytes: dict[str, dict[str, int]] = {}
        # Durable ack floors keyed by (collection, key, durable) — the resume
        # point a re-opened consumer continues from (REQ-021).
        self._consumer_floors: dict[tuple[str, str, str], int] = {}

    async def read(self, collection: str, key: str) -> dict[str, Any] | None:
        return self._records.get(collection, {}).get(key)

    async def write(self, collection: str, key: str, data: dict[str, Any]) -> None:
        self._records.setdefault(collection, {})[key] = data

    async def delete(self, collection: str, key: str) -> bool:
        col = self._records.get(collection, {})
        if key in col:
            del col[key]
            return True
        return False

    async def append(self, collection: str, key: str, entry: dict[str, Any]) -> int:
        self._streams.setdefault(collection, {}).setdefault(key, [])
        self._stream_bytes.setdefault(collection, {}).setdefault(key, 0)
        offset = self._stream_bytes[collection][key]
        line_size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1
        self._stream_bytes[collection][key] += line_size
        self._streams[collection][key].append(entry)
        return offset

    async def append_auto_seq(
        self,
        collection: str,
        key: str,
        entry: dict[str, Any],
    ) -> tuple[int, int]:
        """Atomically assign seq and append. Returns (seq, byte_offset)."""
        self._streams.setdefault(collection, {}).setdefault(key, [])
        self._stream_bytes.setdefault(collection, {}).setdefault(key, 0)
        entries = self._streams[collection][key]
        last_seq = entries[-1].get("seq", 0) if entries else 0
        seq = last_seq + 1
        entry["seq"] = seq
        offset = self._stream_bytes[collection][key]
        line_size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1
        self._stream_bytes[collection][key] += line_size
        entries.append(entry)
        return seq, offset

    async def read_stream(
        self,
        collection: str,
        key: str,
        after_seq: int = 0,
        byte_pos: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        entries = self._streams.get(collection, {}).get(key, [])
        results: list[dict[str, Any]] = []
        cumulative_bytes = 0
        for entry in entries:
            line_size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1
            # Skip entries before byte_pos when byte_pos is used
            if byte_pos > 0 and cumulative_bytes < byte_pos:
                cumulative_bytes += line_size
                continue
            cumulative_bytes += line_size
            if entry.get("seq", 0) > after_seq:
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    async def read_last(self, collection: str, key: str) -> dict[str, Any] | None:
        """Return the last entry in the stream, or None."""
        entries = self._streams.get(collection, {}).get(key, [])
        if not entries:
            return None
        return entries[-1]

    async def get_stream_end_byte_pos(self, collection: str, key: str) -> int:
        """Return the current end-of-stream byte offset (SPEC-017 R-005)."""
        return self._stream_bytes.get(collection, {}).get(key, 0)

    async def query(
        self,
        collection: str,
        filters: dict[str, Any] | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        col = self._records.get(collection, {})
        results: list[dict[str, Any]] = []
        for key, data in sorted(col.items()):
            if prefix and not key.startswith(prefix):
                continue
            if filters and not all(data.get(k) == v for k, v in filters.items()):
                continue
            results.append(data)
        return results

    async def list_keys(self, collection: str, prefix: str | None = None) -> list[str]:
        col = self._records.get(collection, {})
        keys = sorted(col.keys())
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        return keys

    async def exists(self, collection: str, key: str) -> bool:
        return key in self._records.get(collection, {})

    async def open_consumer(self, collection: str, key: str, durable: str) -> Consumer:
        """Bind a durable in-memory consumer that resumes from its ack floor."""
        return MemoryConsumer(self._streams, self._consumer_floors, collection, key, durable)
