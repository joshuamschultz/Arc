"""Tests for EventBuffer — bounded deque with periodic flush."""

import asyncio

from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer


class TestEventBuffer:
    def test_push_adds_to_buffer(self):
        cm = ConnectionManager()
        buf = EventBuffer(cm)
        buf.push({"event": "test"})
        assert buf.pending_count == 1

    def test_push_respects_maxlen(self):
        cm = ConnectionManager()
        buf = EventBuffer(cm, maxlen=3)
        for i in range(5):
            buf.push({"i": i})
        assert buf.pending_count == 3  # Oldest 2 dropped

    async def test_flush_sends_batch_to_clients(self):
        cm = ConnectionManager()
        q = cm.create_queue()
        buf = EventBuffer(cm, flush_interval_ms=50)

        buf.push({"event": "a"})
        buf.push({"event": "b"})
        buf.start()

        # Wait for at least one flush
        await asyncio.sleep(0.15)
        buf.stop()

        # Should have received a batch
        msg = q.get_nowait()
        assert "event_batch" in msg
        assert buf.pending_count == 0

    async def test_no_flush_when_no_clients(self):
        cm = ConnectionManager()
        buf = EventBuffer(cm, flush_interval_ms=50)

        buf.push({"event": "lonely"})
        buf.start()
        await asyncio.sleep(0.15)
        buf.stop()

        # No clients → buffer not cleared (nothing to flush to)
        assert buf.pending_count == 1

    async def test_stop_cancels_flush_task(self):
        cm = ConnectionManager()
        buf = EventBuffer(cm, flush_interval_ms=50)

        buf.start()
        assert buf._task is not None
        buf.stop()
        assert buf._task is None

    def test_stop_without_start_is_noop(self):
        cm = ConnectionManager()
        buf = EventBuffer(cm)
        buf.stop()  # Should not raise
