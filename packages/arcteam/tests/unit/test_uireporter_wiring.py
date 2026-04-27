"""Tests for UIReporter wiring in EntityRegistry and MessagingService.

These tests use a duck-typed fake reporter (no arcui import) to verify
emit_team_event is called at entity registration and message routing.
"""

from __future__ import annotations

from typing import Any

from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message


class FakeReporter:
    """Duck-typed fake that records emit_team_event calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def emit_team_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        self.calls.append({"event_type": event_type, "data": data})


async def _make_registry(reporter: Any | None = None) -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    return EntityRegistry(backend, audit, ui_reporter=reporter)


async def _make_svc(reporter: Any | None = None) -> MessagingService:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    await registry.register(
        Entity(id="agent://sender", name="Sender", type=EntityType.AGENT, roles=[])
    )
    await registry.register(
        Entity(id="agent://recipient", name="Recipient", type=EntityType.AGENT, roles=[])
    )
    return MessagingService(backend, registry, audit, ui_reporter=reporter)


class TestEntityRegisterEmitsUIEvent:
    """EntityRegistry.register() should call emit_team_event when reporter is wired."""

    async def test_register_emits_entity_register_event(self) -> None:
        """emit_team_event is called with event_type='entity_register' on registration."""
        reporter = FakeReporter()
        registry = await _make_registry(reporter=reporter)

        entity = Entity(
            id="agent://alpha",
            name="Alpha",
            type=EntityType.AGENT,
            roles=["ops"],
        )
        await registry.register(entity)

        assert len(reporter.calls) == 1
        call = reporter.calls[0]
        assert call["event_type"] == "entity_register"
        data = call["data"]
        assert data["entity_id"] == "agent://alpha"
        assert data["entity_name"] == "Alpha"

    async def test_no_reporter_register_does_not_raise(self) -> None:
        """No reporter wired — register() works fine without UIReporter."""
        registry = await _make_registry(reporter=None)
        entity = Entity(
            id="agent://beta",
            name="Beta",
            type=EntityType.AGENT,
            roles=[],
        )
        # Should not raise — reporter is optional
        await registry.register(entity)

    async def test_reporter_receives_did_in_data(self) -> None:
        """entity_id is passed in data payload for identity tracing."""
        reporter = FakeReporter()
        registry = await _make_registry(reporter=reporter)

        entity = Entity(
            id="agent://gamma",
            name="Gamma",
            type=EntityType.AGENT,
            roles=["dev"],
        )
        await registry.register(entity)

        assert reporter.calls[0]["data"]["entity_id"] == "agent://gamma"


class TestMessageRouteEmitsUIEvent:
    """MessagingService.send() should call emit_team_event when reporter is wired."""

    async def test_send_emits_message_route_event(self) -> None:
        """emit_team_event is called with event_type='message_route' on send."""
        reporter = FakeReporter()
        svc = await _make_svc(reporter=reporter)

        msg = Message(
            sender="agent://sender",
            to=["agent://recipient"],
            body="hello",
        )
        await svc.send(msg)

        route_calls = [c for c in reporter.calls if c["event_type"] == "message_route"]
        assert len(route_calls) >= 1
        data = route_calls[0]["data"]
        assert "to" in data
        assert "agent://recipient" in data["to"]
        assert "message_id" in data

    async def test_no_reporter_send_does_not_raise(self) -> None:
        """No reporter wired — send() works fine without UIReporter."""
        svc = await _make_svc(reporter=None)
        msg = Message(
            sender="agent://sender",
            to=["agent://recipient"],
            body="hello",
        )
        sent = await svc.send(msg)
        assert sent.seq >= 1

    async def test_send_multi_target_emits_per_route(self) -> None:
        """One route event is emitted per recipient URI in message.to."""
        reporter = FakeReporter()

        # Need channel for multi-target test
        backend = MemoryBackend()
        audit = AuditLogger(backend, hmac_key=b"test-key")
        await audit.initialize()
        registry = EntityRegistry(backend, audit)
        await registry.register(
            Entity(id="agent://sender", name="Sender", type=EntityType.AGENT, roles=[])
        )
        await registry.register(
            Entity(id="agent://recipient", name="Recipient", type=EntityType.AGENT, roles=[])
        )
        svc = MessagingService(backend, registry, audit, ui_reporter=reporter)
        await svc.create_channel(
            Channel(name="test-ch", members=["agent://sender", "agent://recipient"])
        )

        msg = Message(
            sender="agent://sender",
            to=["agent://recipient", "channel://test-ch"],
            body="multi-target",
        )
        await svc.send(msg)

        route_calls = [c for c in reporter.calls if c["event_type"] == "message_route"]
        assert len(route_calls) == 2
