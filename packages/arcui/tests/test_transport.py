"""Tests for UITransport Protocol and InMemoryTransport."""

from __future__ import annotations

import asyncio

import pytest

from arcui.transport import InMemoryTransport, UITransport
from arcui.types import ControlMessage, ControlResponse, UIEvent


class TestInMemoryTransportSendReceive:
    async def test_send_event_and_receive(self):
        client, server = InMemoryTransport.create_pair()
        event = UIEvent(
            layer="llm",
            event_type="trace_record",
            agent_id="agent-001",
            agent_name="researcher",
            source_id="call-abc",
            timestamp="2026-03-03T12:00:00+00:00",
            data={"model": "gpt-4"},
            sequence=0,
        )
        await client.send_event("agent-001", event)
        agent_id, received = await server.receive()
        assert agent_id == "agent-001"
        assert isinstance(received, UIEvent)
        assert received.layer == "llm"
        assert received.sequence == 0

    async def test_send_control_and_receive(self):
        client, server = InMemoryTransport.create_pair()
        msg = ControlMessage(
            action="cancel",
            target="agent-001",
            data={},
            request_id="req-001",
        )
        await server.send_control("agent-001", msg)
        agent_id, received = await client.receive()
        assert agent_id == "agent-001"
        assert isinstance(received, ControlMessage)
        assert received.action == "cancel"

    async def test_send_response_and_receive(self):
        client, server = InMemoryTransport.create_pair()
        resp = ControlResponse(
            request_id="req-001",
            status="ok",
            data={"done": True},
        )
        await client.send_event("agent-001", resp)
        agent_id, received = await server.receive()
        assert agent_id == "agent-001"
        assert isinstance(received, ControlResponse)

    async def test_multiple_messages_ordered(self):
        client, server = InMemoryTransport.create_pair()
        for i in range(5):
            event = UIEvent(
                layer="llm",
                event_type="test",
                agent_id="a",
                agent_name="b",
                source_id="c",
                timestamp="2026-03-03T12:00:00+00:00",
                data={"i": i},
                sequence=i,
            )
            await client.send_event("a", event)

        for i in range(5):
            _, received = await server.receive()
            assert isinstance(received, UIEvent)
            assert received.data["i"] == i

    async def test_bidirectional(self):
        """Both sides can send and receive."""
        client, server = InMemoryTransport.create_pair()

        # Client sends event
        event = UIEvent(
            layer="agent",
            event_type="ready",
            agent_id="a",
            agent_name="b",
            source_id="c",
            timestamp="2026-03-03T12:00:00+00:00",
            data={},
            sequence=0,
        )
        await client.send_event("a", event)

        # Server sends control
        ctrl = ControlMessage(
            action="ping", target="a", data={}, request_id="r1"
        )
        await server.send_control("a", ctrl)

        # Server receives event
        _, recv_event = await server.receive()
        assert isinstance(recv_event, UIEvent)

        # Client receives control
        _, recv_ctrl = await client.receive()
        assert isinstance(recv_ctrl, ControlMessage)


class TestInMemoryTransportClose:
    async def test_close_stops_receive(self):
        client, _server = InMemoryTransport.create_pair()
        await client.close()
        # After close, receive should raise or return sentinel
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await asyncio.wait_for(client.receive(), timeout=0.1)

    async def test_close_is_idempotent(self):
        client, _ = InMemoryTransport.create_pair()
        await client.close()
        await client.close()  # Should not raise


class TestUITransportProtocol:
    def test_in_memory_implements_protocol(self):
        """InMemoryTransport must satisfy UITransport protocol."""
        client, _ = InMemoryTransport.create_pair()
        assert isinstance(client, UITransport)
