"""Unit tests for NatsBackend over a fake JetStream (no server required)."""

from __future__ import annotations

import pytest

from arcteam.backends.nats import NatsBackend
from arcteam.storage import StorageBackend
from tests.unit.backends.fake_jetstream import FakeJetStream

pytestmark = pytest.mark.asyncio

STREAMS = "messages/streams"


@pytest.fixture
def js() -> FakeJetStream:
    return FakeJetStream()


@pytest.fixture
def backend(js: FakeJetStream) -> NatsBackend:
    return NatsBackend(js)


class TestProtocolConformance:
    async def test_is_storage_backend(self, backend: NatsBackend) -> None:
        assert isinstance(backend, StorageBackend)


class TestRecords:
    async def test_write_then_read(self, backend: NatsBackend) -> None:
        await backend.write("reg", "did:arc:local:agent/a1", {"handle": "a1"})
        got = await backend.read("reg", "did:arc:local:agent/a1")
        assert got == {"handle": "a1"}

    async def test_read_missing_returns_none(self, backend: NatsBackend) -> None:
        assert await backend.read("reg", "nope") is None

    async def test_read_missing_bucket_returns_none(self, backend: NatsBackend) -> None:
        assert await backend.read("never-created", "x") is None

    async def test_delete_reports_existence(self, backend: NatsBackend) -> None:
        await backend.write("reg", "k1", {"v": 1})
        assert await backend.delete("reg", "k1") is True
        assert await backend.delete("reg", "k1") is False
        assert await backend.read("reg", "k1") is None

    async def test_exists(self, backend: NatsBackend) -> None:
        assert await backend.exists("reg", "k1") is False
        await backend.write("reg", "k1", {"v": 1})
        assert await backend.exists("reg", "k1") is True

    async def test_query_and_list_keys_roundtrip_original_keys(self, backend: NatsBackend) -> None:
        await backend.write("reg", "did:arc:local:agent/a1", {"role": "ops"})
        await backend.write("reg", "did:arc:local:agent/a2", {"role": "dev"})
        keys = await backend.list_keys("reg")
        assert set(keys) == {"did:arc:local:agent/a1", "did:arc:local:agent/a2"}
        rows = await backend.query("reg", filters={"role": "ops"})
        assert rows == [{"role": "ops"}]

    async def test_query_empty_bucket(self, backend: NatsBackend) -> None:
        assert await backend.query("empty") == []
        assert await backend.list_keys("empty") == []


class TestStreams:
    async def test_append_auto_seq_is_monotonic(self, backend: NatsBackend) -> None:
        seq1, _ = await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": "one"})
        seq2, _ = await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": "two"})
        assert seq1 == 1
        assert seq2 == 2

    async def test_read_stream_after_seq(self, backend: NatsBackend) -> None:
        for i in range(3):
            await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": str(i)})
        rows = await backend.read_stream(STREAMS, "arc.agent.a1", after_seq=1)
        assert [r["body"] for r in rows] == ["1", "2"]
        assert [r["seq"] for r in rows] == [2, 3]

    async def test_read_stream_limit(self, backend: NatsBackend) -> None:
        for i in range(5):
            await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": str(i)})
        rows = await backend.read_stream(STREAMS, "arc.agent.a1", after_seq=0, limit=2)
        assert len(rows) == 2

    async def test_read_last(self, backend: NatsBackend) -> None:
        assert await backend.read_last(STREAMS, "arc.agent.a1") is None
        await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": "one"})
        await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": "two"})
        last = await backend.read_last(STREAMS, "arc.agent.a1")
        assert last is not None
        assert last["body"] == "two"
        assert last["seq"] == 2

    async def test_append_returns_seq(self, backend: NatsBackend) -> None:
        offset = await backend.append("audit", "audit", {"event": "x"})
        assert offset == 1


class TestDurableConsumer:
    """REQ-021: durable consumer = push live AND resume-from-last-ack on restart."""

    async def test_resume_from_last_ack(self, backend: NatsBackend) -> None:
        for i in range(3):
            await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": str(i)})

        consumer = await backend.open_consumer(STREAMS, "arc.agent.a1", "a1-inbox")
        first = await consumer.fetch(2)
        assert [m.data["body"] for m in first] == ["0", "1"]
        for m in first:
            await m.ack()

        # "Restart": rebind the same durable — the ack floor is server-owned,
        # so we resume with zero missed or duplicated messages.
        resumed = await backend.open_consumer(STREAMS, "arc.agent.a1", "a1-inbox")
        rest = await resumed.fetch(10)
        assert [m.data["body"] for m in rest] == ["2"]

    async def test_fetch_empty_returns_empty_list(self, backend: NatsBackend) -> None:
        await backend.append_auto_seq(STREAMS, "arc.agent.a1", {"body": "0"})
        consumer = await backend.open_consumer(STREAMS, "arc.agent.a1", "a1-inbox")
        drained = await consumer.fetch(10)
        for m in drained:
            await m.ack()
        assert await consumer.fetch(10) == []
