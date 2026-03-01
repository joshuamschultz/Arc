"""Tests for ConnectionManager — per-client queue broadcast."""

import asyncio

from arcui.connection import ConnectionManager


class TestConnectionManager:
    def test_create_queue_registers_client(self):
        cm = ConnectionManager()
        assert cm.client_count == 0

        q = cm.create_queue()
        assert cm.client_count == 1
        assert isinstance(q, asyncio.Queue)

    def test_unregister_removes_client(self):
        cm = ConnectionManager()
        q = cm.create_queue()
        assert cm.client_count == 1

        cm.unregister(q)
        assert cm.client_count == 0

    def test_unregister_unknown_queue_is_noop(self):
        cm = ConnectionManager()
        q: asyncio.Queue[str] = asyncio.Queue()
        cm.unregister(q)  # Should not raise
        assert cm.client_count == 0

    def test_broadcast_sends_to_all_clients(self):
        cm = ConnectionManager()
        q1 = cm.create_queue()
        q2 = cm.create_queue()

        cm.broadcast({"type": "test", "value": 42})

        msg1 = q1.get_nowait()
        msg2 = q2.get_nowait()
        assert msg1 == msg2
        assert '"type"' in msg1
        assert '"test"' in msg1

    def test_broadcast_string_passthrough(self):
        cm = ConnectionManager()
        q = cm.create_queue()

        cm.broadcast("raw-string")
        assert q.get_nowait() == "raw-string"

    def test_broadcast_drops_oldest_on_full_queue(self):
        cm = ConnectionManager(maxsize=2)
        q = cm.create_queue()

        cm.broadcast("msg-1")
        cm.broadcast("msg-2")
        # Queue is now full
        cm.broadcast("msg-3")  # Should drop msg-1

        assert q.get_nowait() == "msg-2"
        assert q.get_nowait() == "msg-3"

    def test_broadcast_no_clients_is_noop(self):
        cm = ConnectionManager()
        cm.broadcast({"data": "ignored"})  # Should not raise

    def test_multiple_unregister_is_safe(self):
        cm = ConnectionManager()
        q = cm.create_queue()
        cm.unregister(q)
        cm.unregister(q)  # Second unregister is noop
        assert cm.client_count == 0
