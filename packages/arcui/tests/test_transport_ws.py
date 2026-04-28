"""Tests for WebSocketTransport — client-side WS transport with reconnect."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from arcui.transport_ws import WebSocketTransport, _decorrelated_jitter
from arcui.types import ControlMessage, UIEvent


def _make_event(seq: int = 0, **kwargs) -> UIEvent:
    defaults = {
        "layer": "llm",
        "event_type": "test_event",
        "agent_id": "a",
        "agent_name": "b",
        "source_id": "c",
        "timestamp": "2026-03-03T12:00:00+00:00",
        "data": {},
        "sequence": seq,
    }
    defaults.update(kwargs)
    return UIEvent(**defaults)


class TestDecorrelatedJitter:
    """Test the backoff algorithm independently."""

    def test_first_sleep_between_base_and_3x_base(self):
        base, cap = 1.0, 60.0
        for _ in range(100):
            sleep = _decorrelated_jitter(base, cap, base)
            assert base <= sleep <= base * 3

    def test_capped_at_max(self):
        base, cap = 1.0, 60.0
        prev = 100.0
        for _ in range(100):
            sleep = _decorrelated_jitter(base, cap, prev)
            assert sleep <= cap

    def test_never_below_base(self):
        base, cap = 1.0, 60.0
        for _ in range(100):
            sleep = _decorrelated_jitter(base, cap, 0.1)
            assert sleep >= base

    def test_deterministic_with_seed(self):
        import random

        base, cap = 1.0, 60.0
        random.seed(42)
        a = _decorrelated_jitter(base, cap, 5.0)
        random.seed(42)
        b = _decorrelated_jitter(base, cap, 5.0)
        assert a == b


class TestWebSocketTransportBuffer:
    """Test local event buffer behavior."""

    async def test_buffer_stores_events_when_disconnected(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
            buffer_size=10,
        )
        event = _make_event()
        transport.buffer_event("a", event)
        assert transport.buffer_size == 1

    async def test_buffer_drops_oldest_when_full(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
            buffer_size=3,
        )
        for i in range(5):
            transport.buffer_event("a", _make_event(seq=i, data={"i": i}))

        assert transport.buffer_size == 3
        items = transport.drain_buffer()
        assert len(items) == 3
        assert items[0][1].data["i"] == 2

    async def test_drain_buffer_empties(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
            buffer_size=10,
        )
        event = _make_event()
        transport.buffer_event("a", event)
        transport.buffer_event("a", event)
        items = transport.drain_buffer()
        assert len(items) == 2
        assert transport.buffer_size == 0


class TestWebSocketTransportConfig:
    def test_default_config(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        assert transport.url == "ws://localhost:8420/api/agent/connect"
        assert transport.reconnect_base == 1.0
        assert transport.reconnect_cap == 60.0
        assert transport._max_buffer == 1000

    def test_custom_config(self):
        transport = WebSocketTransport(
            url="ws://example.com/ws",
            token="my-token",
            reconnect_base=2.0,
            reconnect_cap=30.0,
            buffer_size=500,
        )
        assert transport.reconnect_base == 2.0
        assert transport.reconnect_cap == 30.0
        assert transport._max_buffer == 500


class TestWebSocketTransportSend:
    @pytest.mark.asyncio
    async def test_send_event_when_connected(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        mock_ws = AsyncMock()
        transport._ws = mock_ws
        transport._closed = False

        event = _make_event()
        await transport.send_event("agent-1", event)
        mock_ws.send.assert_called_once()

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["agent_id"] == "agent-1"
        assert sent["type"] == "event"

    @pytest.mark.asyncio
    async def test_send_event_buffers_when_disconnected(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        event = _make_event()
        await transport.send_event("agent-1", event)
        assert transport.buffer_size == 1

    @pytest.mark.asyncio
    async def test_send_event_exception_buffers(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = ConnectionError("broken")
        transport._ws = mock_ws
        transport._closed = False

        event = _make_event()
        await transport.send_event("agent-1", event)
        assert transport.buffer_size == 1

    @pytest.mark.asyncio
    async def test_send_control_disconnected_raises(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        msg = ControlMessage(
            action="ping", target="a", data={}, request_id="r1"
        )
        with pytest.raises(RuntimeError, match="Not connected"):
            await transport.send_control("agent-1", msg)


class TestWebSocketTransportReceive:
    @pytest.mark.asyncio
    async def test_receive_control_message(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        mock_ws = AsyncMock()
        mock_ws.recv.return_value = json.dumps({
            "agent_id": "a1",
            "type": "control",
            "payload": {
                "action": "cancel",
                "target": "a1",
                "data": {},
                "request_id": "r1",
            },
        })
        transport._ws = mock_ws
        transport._closed = False

        agent_id, msg = await transport.receive()
        assert agent_id == "a1"
        assert isinstance(msg, ControlMessage)
        assert msg.action == "cancel"


class TestWebSocketTransportClose:
    @pytest.mark.asyncio
    async def test_close_sets_closed_flag(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        mock_ws = AsyncMock()
        transport._ws = mock_ws
        transport._closed = False

        await transport.close()
        assert transport._closed is True
        assert transport._ws is None
        mock_ws.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_handles_error(self):
        transport = WebSocketTransport(
            url="ws://localhost:8420/api/agent/connect",
            token="test-token",
        )
        mock_ws = AsyncMock()
        mock_ws.close.side_effect = OSError("already closed")
        transport._ws = mock_ws
        transport._closed = False

        await transport.close()
        assert transport._closed is True
        assert transport._ws is None


class TestTokenProviderRefresh:
    """token_provider re-reads the token before each (re)connect attempt.

    Without this, an arcui restart (which rotates auth tokens) permanently
    breaks every connected agent until the agent itself is restarted.
    """

    def test_no_provider_uses_static_token(self):
        t = WebSocketTransport(url="ws://x", token="static")
        assert t._current_token() == "static"

    def test_provider_returning_new_token_replaces_cached(self):
        tokens = iter(["initial", "rotated"])
        t = WebSocketTransport(
            url="ws://x", token="initial",
            token_provider=lambda: next(tokens),
        )
        # First call exhausts "initial" — same as cached, no swap
        assert t._current_token() == "initial"
        # Second call returns "rotated" — should swap and return new
        assert t._current_token() == "rotated"
        assert t.token == "rotated"

    def test_provider_returning_empty_falls_back_to_cached(self):
        t = WebSocketTransport(
            url="ws://x", token="cached",
            token_provider=lambda: "",
        )
        assert t._current_token() == "cached"

    def test_provider_raising_exception_falls_back_silently(self):
        def boom() -> str:
            raise OSError("file gone")
        t = WebSocketTransport(
            url="ws://x", token="cached",
            token_provider=boom,
        )
        # Should not raise; falls back to cached.
        assert t._current_token() == "cached"
