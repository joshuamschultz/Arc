"""NatsBackend caches one KV handle per bucket (no stream_info per read).

Root cause on the DGX: every ``read``/``list_keys`` re-ran ``js.key_value``
(a JetStream ``stream_info`` round trip), so one channel listing cost 2N+2
requests, each with its own 5 s timeout.
"""

from __future__ import annotations

import pytest
from packages.arcteam.tests.unit.backends.fake_jetstream import FakeJetStream, FakeKV

from arcteam.backends.nats import NatsBackend

pytestmark = pytest.mark.asyncio


class _CountingJetStream(FakeJetStream):
    """Counts the ``key_value`` stream_info round trips the backend makes."""

    def __init__(self) -> None:
        super().__init__()
        self.key_value_calls = 0

    async def key_value(self, bucket: str) -> FakeKV:
        self.key_value_calls += 1
        return await super().key_value(bucket)


class _FakeConnection:
    """The two ``nats.Client`` attributes the backend consults."""

    is_connected = True

    def __init__(self) -> None:
        self.stats = {"reconnects": 0}


class TestKvHandleCache:
    async def test_handle_is_reused_across_reads(self) -> None:
        js = _CountingJetStream()
        await NatsBackend(js).write("reg", "k1", {"v": 1})
        backend = NatsBackend(js)  # cold cache over the existing bucket
        js.key_value_calls = 0

        for _ in range(5):
            await backend.query("reg")

        assert js.key_value_calls == 1

    async def test_missing_bucket_is_not_cached(self) -> None:
        js = _CountingJetStream()
        backend = NatsBackend(js)
        assert await backend.read("late", "k1") is None
        await backend.write("late", "k1", {"v": 1})
        assert await backend.read("late", "k1") == {"v": 1}

    async def test_handle_is_rebuilt_after_reconnect(self) -> None:
        js = _CountingJetStream()
        conn = _FakeConnection()
        backend = NatsBackend(js, conn)  # type: ignore[arg-type]  # reason: fake client
        await backend.write("reg", "k1", {"v": 1})
        await backend.read("reg", "k1")
        js.key_value_calls = 0

        await backend.read("reg", "k1")
        assert js.key_value_calls == 0

        conn.stats["reconnects"] += 1
        await backend.read("reg", "k1")
        assert js.key_value_calls == 1
        await backend.read("reg", "k1")
        assert js.key_value_calls == 1
