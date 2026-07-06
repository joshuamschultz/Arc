"""Tests for arcteam.storage — MemoryBackend (the dependency-free test backend)."""

from __future__ import annotations

import pytest

from arcteam.storage import MemoryBackend, StorageBackend


@pytest.fixture
def backend() -> MemoryBackend:
    return MemoryBackend()


class TestProtocolConformance:
    def test_memory_backend_is_storage_backend(self, backend: MemoryBackend) -> None:
        assert isinstance(backend, StorageBackend)


class TestWriteRead:
    async def test_write_and_read_back(self, backend: StorageBackend) -> None:
        data = {"name": "test", "value": 42}
        await backend.write("col", "key1", data)
        assert await backend.read("col", "key1") == data

    async def test_overwrite(self, backend: StorageBackend) -> None:
        await backend.write("col", "key1", {"v": 1})
        await backend.write("col", "key1", {"v": 2})
        assert await backend.read("col", "key1") == {"v": 2}

    async def test_read_nonexistent(self, backend: StorageBackend) -> None:
        assert await backend.read("col", "missing") is None


class TestDelete:
    async def test_delete_existing(self, backend: StorageBackend) -> None:
        await backend.write("col", "key1", {"v": 1})
        assert await backend.delete("col", "key1") is True
        assert await backend.read("col", "key1") is None

    async def test_delete_nonexistent(self, backend: StorageBackend) -> None:
        assert await backend.delete("col", "missing") is False


class TestAppend:
    async def test_append_returns_offset(self, backend: StorageBackend) -> None:
        offset1 = await backend.append("streams", "s1", {"seq": 1, "data": "first"})
        assert offset1 == 0
        offset2 = await backend.append("streams", "s1", {"seq": 2, "data": "second"})
        assert offset2 > 0


class TestReadStream:
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
        assert await backend.read_stream("streams", "nonexistent") == []


class TestQuery:
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
        assert await backend.query("nonexistent") == []


class TestListKeys:
    async def test_list_all(self, backend: StorageBackend) -> None:
        await backend.write("col", "a", {"v": 1})
        await backend.write("col", "b", {"v": 2})
        assert sorted(await backend.list_keys("col")) == ["a", "b"]

    async def test_list_with_prefix(self, backend: StorageBackend) -> None:
        await backend.write("col", "agent_a1", {"v": 1})
        await backend.write("col", "user_u1", {"v": 2})
        assert await backend.list_keys("col", prefix="agent_") == ["agent_a1"]


class TestReadLast:
    async def test_read_last_empty(self, backend: StorageBackend) -> None:
        assert await backend.read_last("streams", "empty") is None

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


class TestAppendAutoSeq:
    async def test_assigns_seq_from_one(self, backend: StorageBackend) -> None:
        seq, offset = await backend.append_auto_seq("streams", "s1", {"data": "first"})
        assert seq == 1
        assert offset == 0

    async def test_monotonic_seq(self, backend: StorageBackend) -> None:
        for _ in range(5):
            await backend.append_auto_seq("streams", "s1", {"data": "msg"})
        results = await backend.read_stream("streams", "s1", after_seq=0)
        assert [r["seq"] for r in results] == [1, 2, 3, 4, 5]

    async def test_entry_gets_seq_field(self, backend: StorageBackend) -> None:
        entry: dict = {"data": "test"}
        seq, _ = await backend.append_auto_seq("streams", "s1", entry)
        assert entry["seq"] == seq


class TestExists:
    async def test_exists_true(self, backend: StorageBackend) -> None:
        await backend.write("col", "key1", {"v": 1})
        assert await backend.exists("col", "key1") is True

    async def test_exists_false(self, backend: StorageBackend) -> None:
        assert await backend.exists("col", "missing") is False
