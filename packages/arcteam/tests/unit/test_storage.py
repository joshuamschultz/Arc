"""Tests for arcteam.storage — FileBackend and MemoryBackend."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from arcteam.storage import FileBackend, MemoryBackend, StorageBackend


@pytest.fixture
def file_backend(tmp_path: Path) -> FileBackend:
    return FileBackend(root=tmp_path)


@pytest.fixture
def memory_backend() -> MemoryBackend:
    return MemoryBackend()


# Parametrize tests to run against both backends
@pytest.fixture(params=["file", "memory"])
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[StorageBackend]:
    if request.param == "file":
        yield FileBackend(root=tmp_path)
    else:
        yield MemoryBackend()


class TestProtocolConformance:
    """Both backends satisfy the StorageBackend protocol."""

    def test_file_backend_is_storage_backend(self, file_backend: FileBackend) -> None:
        assert isinstance(file_backend, StorageBackend)

    def test_memory_backend_is_storage_backend(self, memory_backend: MemoryBackend) -> None:
        assert isinstance(memory_backend, StorageBackend)


class TestAtomicWrite:
    """Atomic write: verify temp file + rename pattern."""

    async def test_write_and_read_back(self, backend: StorageBackend) -> None:
        data = {"name": "test", "value": 42}
        await backend.write("col", "key1", data)
        result = await backend.read("col", "key1")
        assert result == data

    async def test_overwrite(self, backend: StorageBackend) -> None:
        await backend.write("col", "key1", {"v": 1})
        await backend.write("col", "key1", {"v": 2})
        result = await backend.read("col", "key1")
        assert result == {"v": 2}

    async def test_read_nonexistent(self, backend: StorageBackend) -> None:
        result = await backend.read("col", "missing")
        assert result is None

    async def test_file_backend_atomic_pattern(self, file_backend: FileBackend) -> None:
        """Verify the file is actually created (not left as temp)."""
        await file_backend.write("col", "key1", {"test": True})
        path = file_backend._record_path("col", "key1")
        assert path.exists()
        assert path.suffix == ".json"
        # No .tmp files left behind
        temps = list(path.parent.glob("*.tmp"))
        assert len(temps) == 0


class TestDelete:
    """Delete operations."""

    async def test_delete_existing(self, backend: StorageBackend) -> None:
        await backend.write("col", "key1", {"v": 1})
        existed = await backend.delete("col", "key1")
        assert existed is True
        assert await backend.read("col", "key1") is None

    async def test_delete_nonexistent(self, backend: StorageBackend) -> None:
        existed = await backend.delete("col", "missing")
        assert existed is False


class TestAppend:
    """JSONL append with byte offset tracking."""

    async def test_append_returns_offset(self, backend: StorageBackend) -> None:
        offset1 = await backend.append("streams", "s1", {"seq": 1, "data": "first"})
        assert offset1 == 0  # First entry at beginning
        offset2 = await backend.append("streams", "s1", {"seq": 2, "data": "second"})
        assert offset2 > 0  # Second entry after first

    async def test_append_jsonl_format(self, file_backend: FileBackend) -> None:
        """Verify JSONL format: one JSON object per line."""
        await file_backend.append("streams", "s1", {"seq": 1, "data": "a"})
        await file_backend.append("streams", "s1", {"seq": 2, "data": "b"})
        stream_path = file_backend._stream_path("streams", "s1")
        lines = stream_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"seq": 1, "data": "a"}
        assert json.loads(lines[1]) == {"seq": 2, "data": "b"}


class TestReadStream:
    """read_stream: seek, filter, limit, malformed recovery."""

    async def test_read_from_beginning(self, backend: StorageBackend) -> None:
        for i in range(1, 6):
            await backend.append("streams", "s1", {"seq": i, "msg": f"m{i}"})
        results = await backend.read_stream("streams", "s1", after_seq=0)
        assert len(results) == 5
        assert results[0]["seq"] == 1

    async def test_read_after_seq(self, backend: StorageBackend) -> None:
        for i in range(1, 6):
            await backend.append("streams", "s1", {"seq": i, "msg": f"m{i}"})
        results = await backend.read_stream("streams", "s1", after_seq=3)
        assert len(results) == 2
        assert results[0]["seq"] == 4

    async def test_read_with_limit(self, backend: StorageBackend) -> None:
        for i in range(1, 11):
            await backend.append("streams", "s1", {"seq": i})
        results = await backend.read_stream("streams", "s1", after_seq=0, limit=3)
        assert len(results) == 3

    async def test_read_empty_stream(self, backend: StorageBackend) -> None:
        results = await backend.read_stream("streams", "nonexistent")
        assert results == []

    async def test_skip_malformed_lines(self, file_backend: FileBackend) -> None:
        """Crash recovery: malformed trailing lines are skipped."""
        await file_backend.append("streams", "s1", {"seq": 1, "data": "good"})
        # Manually write a malformed line
        stream_path = file_backend._stream_path("streams", "s1")
        with open(stream_path, "a") as f:
            f.write("{incomplete json\n")
        await file_backend.append("streams", "s1", {"seq": 2, "data": "also good"})
        results = await file_backend.read_stream("streams", "s1", after_seq=0)
        assert len(results) == 2
        assert results[0]["data"] == "good"
        assert results[1]["data"] == "also good"

    async def test_read_with_byte_pos(self, file_backend: FileBackend) -> None:
        """Seek to byte_pos for fast reading."""
        await file_backend.append("streams", "s1", {"seq": 1, "data": "first"})
        offset = await file_backend.append("streams", "s1", {"seq": 2, "data": "second"})
        await file_backend.append("streams", "s1", {"seq": 3, "data": "third"})
        results = await file_backend.read_stream("streams", "s1", byte_pos=offset, after_seq=1)
        assert len(results) == 2
        assert results[0]["seq"] == 2


class TestConcurrentAppends:
    """Concurrent appends: 10 writers to same stream, no corruption."""

    async def test_concurrent_writers(self, file_backend: FileBackend) -> None:
        async def writer(seq: int) -> None:
            await file_backend.append("streams", "s1", {"seq": seq, "writer": seq})

        tasks = [writer(i) for i in range(1, 11)]
        await asyncio.gather(*tasks)

        results = await file_backend.read_stream("streams", "s1", after_seq=0, limit=100)
        assert len(results) == 10
        # All records should be valid JSON (no corruption)
        seqs = sorted(r["seq"] for r in results)
        assert seqs == list(range(1, 11))


class TestQuery:
    """Query: field filtering, prefix filtering."""

    async def test_filter_by_field(self, backend: StorageBackend) -> None:
        await backend.write("entities", "a1", {"id": "a1", "type": "agent", "role": "ops"})
        await backend.write("entities", "a2", {"id": "a2", "type": "agent", "role": "dev"})
        await backend.write("entities", "u1", {"id": "u1", "type": "user", "role": "ops"})
        results = await backend.query("entities", filters={"type": "agent"})
        assert len(results) == 2

    async def test_filter_by_prefix(self, backend: StorageBackend) -> None:
        await backend.write("entities", "agent_a1", {"id": "a1"})
        await backend.write("entities", "agent_a2", {"id": "a2"})
        await backend.write("entities", "user_u1", {"id": "u1"})
        results = await backend.query("entities", prefix="agent_")
        assert len(results) == 2

    async def test_query_empty_collection(self, backend: StorageBackend) -> None:
        results = await backend.query("nonexistent")
        assert results == []


class TestListKeys:
    """list_keys operations."""

    async def test_list_all(self, backend: StorageBackend) -> None:
        await backend.write("col", "a", {"v": 1})
        await backend.write("col", "b", {"v": 2})
        keys = await backend.list_keys("col")
        assert sorted(keys) == ["a", "b"]

    async def test_list_with_prefix(self, backend: StorageBackend) -> None:
        await backend.write("col", "agent_a1", {"v": 1})
        await backend.write("col", "user_u1", {"v": 2})
        keys = await backend.list_keys("col", prefix="agent_")
        assert keys == ["agent_a1"]


class TestReadLast:
    """read_last: O(1) retrieval of last stream entry."""

    async def test_read_last_empty(self, backend: StorageBackend) -> None:
        result = await backend.read_last("streams", "empty")
        assert result is None

    async def test_read_last_single(self, backend: StorageBackend) -> None:
        await backend.append("streams", "s1", {"seq": 1, "data": "only"})
        result = await backend.read_last("streams", "s1")
        assert result is not None
        assert result["seq"] == 1

    async def test_read_last_multiple(self, backend: StorageBackend) -> None:
        for i in range(1, 11):
            await backend.append("streams", "s1", {"seq": i, "data": f"m{i}"})
        result = await backend.read_last("streams", "s1")
        assert result is not None
        assert result["seq"] == 10
        assert result["data"] == "m10"


class TestPathSanitization:
    """Path traversal prevention in FileBackend."""

    async def test_reject_dotdot(self, file_backend: FileBackend) -> None:
        with pytest.raises(ValueError, match="Unsafe path"):
            await file_backend.read("../etc", "passwd")

    async def test_reject_absolute_path(self, file_backend: FileBackend) -> None:
        with pytest.raises(ValueError, match="Unsafe path"):
            await file_backend.read("/etc", "passwd")

    async def test_reject_dotdot_in_key(self, file_backend: FileBackend) -> None:
        with pytest.raises(ValueError, match="Unsafe path"):
            await file_backend.write("col", "../escape", {"bad": True})

    async def test_valid_nested_path(self, file_backend: FileBackend) -> None:
        """Nested paths with / are allowed (cursor keys use this)."""
        await file_backend.write("cursors", "arc.channel.ops/agent_a1", {"seq": 5})
        result = await file_backend.read("cursors", "arc.channel.ops/agent_a1")
        assert result == {"seq": 5}


class TestAppendAutoSeq:
    """append_auto_seq: atomic seq assignment prevents duplicates."""

    async def test_assigns_seq_from_one(self, backend: StorageBackend) -> None:
        seq, offset = await backend.append_auto_seq("streams", "s1", {"data": "first"})
        assert seq == 1
        assert offset == 0

    async def test_monotonic_seq(self, backend: StorageBackend) -> None:
        for _ in range(5):
            await backend.append_auto_seq("streams", "s1", {"data": "msg"})
        results = await backend.read_stream("streams", "s1", after_seq=0)
        seqs = [r["seq"] for r in results]
        assert seqs == [1, 2, 3, 4, 5]

    async def test_entry_gets_seq_field(self, backend: StorageBackend) -> None:
        entry: dict = {"data": "test"}
        seq, _ = await backend.append_auto_seq("streams", "s1", entry)
        assert entry["seq"] == seq

    async def test_concurrent_auto_seq(self, file_backend: FileBackend) -> None:
        """Concurrent appends get unique seq numbers under flock."""
        results: list[int] = []

        async def writer() -> None:
            seq, _ = await file_backend.append_auto_seq(
                "streams",
                "s1",
                {"data": "msg"},
            )
            results.append(seq)

        tasks = [writer() for _ in range(10)]
        await asyncio.gather(*tasks)

        assert sorted(results) == list(range(1, 11))


class TestExists:
    """exists operations."""

    async def test_exists_true(self, backend: StorageBackend) -> None:
        await backend.write("col", "key1", {"v": 1})
        assert await backend.exists("col", "key1") is True

    async def test_exists_false(self, backend: StorageBackend) -> None:
        assert await backend.exists("col", "missing") is False
