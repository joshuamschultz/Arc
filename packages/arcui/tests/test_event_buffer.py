"""Tests for EventBuffer — bounded deque with periodic flush."""

import asyncio

from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.subscription import Subscription, SubscriptionManager
from arcui.types import UIEvent


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


def _make_event(agent_id: str = "a1", layer: str = "llm") -> UIEvent:
    return UIEvent(
        layer=layer,
        event_type="test",
        agent_id=agent_id,
        agent_name="test",
        source_id="src",
        timestamp="2026-03-03T12:00:00+00:00",
        data={},
        sequence=0,
    )


class TestEventBufferUIEvent:
    def test_push_accepts_ui_event(self):
        cm = ConnectionManager()
        buf = EventBuffer(cm)
        event = _make_event()
        buf.push(event)
        assert buf.pending_count == 1

    async def test_flush_with_subscription_manager(self):
        cm = ConnectionManager()
        sub_mgr = SubscriptionManager()
        buf = EventBuffer(cm, subscription_manager=sub_mgr, flush_interval_ms=50)

        q1: asyncio.Queue[str] = asyncio.Queue()
        q2: asyncio.Queue[str] = asyncio.Queue()
        sub_mgr.set_subscription(q1, Subscription(agents=["a1"]))
        sub_mgr.set_subscription(q2, Subscription(agents=["a2"]))

        buf.push(_make_event(agent_id="a1"))
        buf.start()
        await asyncio.sleep(0.15)
        buf.stop()

        # q1 matches, q2 doesn't
        assert not q1.empty()
        assert q2.empty()

    async def test_flush_falls_back_to_cm_for_raw_dicts(self):
        """Raw dicts should still go through ConnectionManager.broadcast."""
        cm = ConnectionManager()
        q = cm.create_queue()
        buf = EventBuffer(cm, flush_interval_ms=50)

        buf.push({"event": "legacy"})
        buf.start()
        await asyncio.sleep(0.15)
        buf.stop()

        assert not q.empty()
