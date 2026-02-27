"""Storage backend protocol and implementations for ArcTeam messaging."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Reject path components that could escape the root directory
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_./:-]+$")


def _sanitize(name: str) -> str:
    """Sanitize a collection or key name to prevent path traversal."""
    if ".." in name or name.startswith("/"):
        raise ValueError(f"Unsafe path component: {name!r}")
    if not _SAFE_NAME.match(name):
        raise ValueError(f"Invalid characters in path component: {name!r}")
    return name


@runtime_checkable
class StorageBackend(Protocol):
    """Swappable storage abstraction. Phase 1: files. Phase 2: SQLite. Phase 4: Postgres."""

    async def read(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read a single JSON record."""
        ...

    async def write(self, collection: str, key: str, data: dict[str, Any]) -> None:
        """Write/overwrite a single JSON record. Atomic (write-to-tmp + rename)."""
        ...

    async def delete(self, collection: str, key: str) -> bool:
        """Delete a record. Returns True if it existed."""
        ...

    async def append(self, collection: str, key: str, entry: dict[str, Any]) -> int:
        """Append to a JSONL stream. Returns byte offset. File-locked."""
        ...

    async def read_stream(
        self,
        collection: str,
        key: str,
        after_seq: int = 0,
        byte_pos: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read entries from a JSONL stream starting after after_seq."""
        ...

    async def read_last(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read the last entry from a JSONL stream. O(1) for file backend."""
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
        """Atomically assign seq and append. Returns (seq, byte_offset).

        Reads last seq under file lock, increments, writes — prevents
        duplicate seq numbers when multiple processes share a stream.
        """
        ...

    async def exists(self, collection: str, key: str) -> bool:
        """Check if a record exists."""
        ...


class FileBackend:
    """File-based storage backend using JSON files and JSONL streams."""

    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def _record_path(self, collection: str, key: str) -> Path:
        return self._root / _sanitize(collection) / f"{_sanitize(key)}.json"

    def _stream_dir(self, collection: str, key: str) -> Path:
        return self._root / _sanitize(collection) / _sanitize(key)

    def _stream_path(self, collection: str, key: str) -> Path:
        return self._stream_dir(collection, key) / "00000000.log"

    # --- Sync helpers (run via asyncio.to_thread) ---

    def _sync_read(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def _sync_write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _sync_delete(self, path: Path) -> bool:
        if path.exists():
            path.unlink()
            return True
        return False

    def _sync_append(self, stream: Path, entry: dict[str, Any]) -> int:
        stream.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        with open(stream, "ab") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                offset = f.seek(0, os.SEEK_END)
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return offset

    def _sync_append_auto_seq(
        self,
        stream: Path,
        entry: dict[str, Any],
    ) -> tuple[int, int]:
        """Assign seq and append atomically under flock.

        Reads the last seq number and writes the new entry under a single
        exclusive file lock, preventing duplicate seq numbers when multiple
        processes share a stream.
        """
        stream.parent.mkdir(parents=True, exist_ok=True)
        with open(stream, "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                # Determine last seq under the lock
                size = f.seek(0, os.SEEK_END)
                last_seq = 0
                if size > 0:
                    last_seq = self._parse_last_seq_from_handle(f, size)
                seq = last_seq + 1
                entry["seq"] = seq
                line = json.dumps(entry, ensure_ascii=False) + "\n"
                encoded = line.encode("utf-8")
                offset = f.seek(0, os.SEEK_END)
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return seq, offset

    @staticmethod
    def _parse_last_seq_from_handle(f: Any, size: int) -> int:
        """Extract seq from the last valid line of an open JSONL file handle."""
        pos = size - 1
        f.seek(pos)
        if f.read(1) == b"\n":
            pos -= 1
        while pos > 0:
            f.seek(pos)
            if f.read(1) == b"\n":
                break
            pos -= 1
        f.seek(pos + 1 if pos > 0 else 0)
        last_line = f.readline().decode("utf-8").strip()
        if last_line:
            try:
                return json.loads(last_line).get("seq", 0)
            except json.JSONDecodeError:
                return 0
        return 0

    def _sync_read_stream(
        self, stream: Path, after_seq: int, byte_pos: int, limit: int
    ) -> list[dict[str, Any]]:
        if not stream.exists():
            return []
        results: list[dict[str, Any]] = []
        with open(stream, encoding="utf-8") as f:
            if byte_pos > 0:
                f.seek(byte_pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("seq", 0) > after_seq:
                    results.append(record)
                    if len(results) >= limit:
                        break
        return results

    def _sync_read_last(self, stream: Path) -> dict[str, Any] | None:
        """Read last line of a JSONL file by seeking backward from EOF."""
        if not stream.exists():
            return None
        size = stream.stat().st_size
        if size == 0:
            return None
        with open(stream, "rb") as f:
            # Seek backward to find the last complete line
            pos = size - 1
            # Skip trailing newline
            if pos > 0:
                f.seek(pos)
                if f.read(1) == b"\n":
                    pos -= 1
            # Walk backward to find previous newline
            while pos > 0:
                f.seek(pos)
                if f.read(1) == b"\n":
                    break
                pos -= 1
            # Read from start of last line
            if pos > 0:
                f.seek(pos + 1)
            else:
                f.seek(0)
            last_line = f.readline().decode("utf-8").strip()
        if not last_line:
            return None
        try:
            return json.loads(last_line)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return None

    def _sync_query(
        self, col_dir: Path, filters: dict[str, Any] | None, prefix: str | None
    ) -> list[dict[str, Any]]:
        if not col_dir.exists():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(col_dir.glob("*.json")):
            key = path.stem
            if prefix and not key.startswith(prefix):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if filters:
                if not all(data.get(k) == v for k, v in filters.items()):
                    continue
            results.append(data)
        return results

    def _sync_list_keys(self, col_dir: Path, prefix: str | None) -> list[str]:
        if not col_dir.exists():
            return []
        keys: list[str] = []
        # Use rglob to find nested JSON files (e.g., cursor keys with / in path)
        for path in sorted(col_dir.rglob("*.json")):
            key = str(path.relative_to(col_dir).with_suffix(""))
            if prefix and not key.startswith(prefix):
                continue
            keys.append(key)
        return keys

    # --- Async interface (delegates to thread pool) ---

    async def read(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read a single JSON record."""
        path = self._record_path(collection, key)
        return await asyncio.to_thread(self._sync_read, path)

    async def write(self, collection: str, key: str, data: dict[str, Any]) -> None:
        """Atomic write: temp file + os.replace()."""
        path = self._record_path(collection, key)
        await asyncio.to_thread(self._sync_write, path, data)

    async def delete(self, collection: str, key: str) -> bool:
        """Delete a record. Returns True if it existed."""
        path = self._record_path(collection, key)
        return await asyncio.to_thread(self._sync_delete, path)

    async def append(self, collection: str, key: str, entry: dict[str, Any]) -> int:
        """Append to JSONL stream with flock. Returns byte offset before append."""
        stream = self._stream_path(collection, key)
        return await asyncio.to_thread(self._sync_append, stream, entry)

    async def append_auto_seq(
        self,
        collection: str,
        key: str,
        entry: dict[str, Any],
    ) -> tuple[int, int]:
        """Atomically assign seq and append. Returns (seq, byte_offset)."""
        stream = self._stream_path(collection, key)
        return await asyncio.to_thread(self._sync_append_auto_seq, stream, entry)

    async def read_stream(
        self,
        collection: str,
        key: str,
        after_seq: int = 0,
        byte_pos: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read entries from JSONL stream. Seeks to byte_pos, filters by after_seq."""
        stream = self._stream_path(collection, key)
        return await asyncio.to_thread(self._sync_read_stream, stream, after_seq, byte_pos, limit)

    async def read_last(self, collection: str, key: str) -> dict[str, Any] | None:
        """Read the last entry from a JSONL stream. O(1) via backward seek."""
        stream = self._stream_path(collection, key)
        return await asyncio.to_thread(self._sync_read_last, stream)

    async def query(
        self,
        collection: str,
        filters: dict[str, Any] | None = None,
        prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query records by field match or key prefix."""
        col_dir = self._root / _sanitize(collection)
        return await asyncio.to_thread(self._sync_query, col_dir, filters, prefix)

    async def list_keys(self, collection: str, prefix: str | None = None) -> list[str]:
        """List all keys (file stems) in a collection, including nested paths."""
        col_dir = self._root / _sanitize(collection)
        return await asyncio.to_thread(self._sync_list_keys, col_dir, prefix)

    async def exists(self, collection: str, key: str) -> bool:
        """Check if a record exists."""
        path = self._record_path(collection, key)
        return await asyncio.to_thread(path.exists)


class MemoryBackend:
    """In-memory storage backend for unit tests. Dict-backed, no filesystem."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._streams: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._stream_bytes: dict[str, dict[str, int]] = {}

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
