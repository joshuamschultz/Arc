"""SPEC-038 REQ-024/026 — messenger no-write-down (Bell-LaPadula star property)."""

from __future__ import annotations

import pytest
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message


async def _svc(*, strict: bool = False) -> MessagingService:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    svc = MessagingService(backend, registry, audit, strict_classification=strict)
    await registry.register(
        Entity(
            did="did:arc:test:agent/hi",
            handle="hi",
            id="agent://hi",
            name="High",
            type=EntityType.AGENT,
            clearance="SECRET",
        )
    )
    await registry.register(
        Entity(
            did="did:arc:test:agent/lo",
            handle="lo",
            id="agent://lo",
            name="Low",
            type=EntityType.AGENT,
            clearance="CUI",
        )
    )
    await registry.register(
        Entity(
            did="did:arc:test:agent/sender",
            handle="sender",
            id="agent://sender",
            name="Sender",
            type=EntityType.AGENT,
            clearance="SECRET",
        )
    )
    await svc.create_channel(
        Channel(name="lowchan", members=["agent://sender", "agent://lo"], clearance="CUI")
    )
    return svc


class TestNoWriteDown:
    async def test_secret_message_to_cui_recipient_refused(self) -> None:
        svc = await _svc()
        msg = Message(sender="agent://sender", to=["agent://lo"], body="x", classification="SECRET")
        with pytest.raises(ValueError, match="classification"):
            await svc.send(msg)

    async def test_secret_message_to_secret_recipient_delivered(self) -> None:
        svc = await _svc()
        msg = Message(sender="agent://sender", to=["agent://hi"], body="x", classification="SECRET")
        sent = await svc.send(msg)
        assert sent.status == "sent"

    async def test_secret_message_to_cui_channel_refused(self) -> None:
        svc = await _svc()
        msg = Message(
            sender="agent://sender", to=["channel://lowchan"], body="x", classification="SECRET"
        )
        with pytest.raises(ValueError, match="classification"):
            await svc.send(msg)

    async def test_default_unclassified_message_delivered(self) -> None:
        svc = await _svc()
        msg = Message(sender="agent://sender", to=["agent://lo"], body="x")
        sent = await svc.send(msg)
        assert sent.status == "sent"

    async def test_federal_unknown_recipient_clearance_denied(self) -> None:
        svc = await _svc(strict=True)
        # Unregistered recipient → clearance cannot be resolved → fail closed.
        msg = Message(
            sender="agent://sender", to=["agent://ghost"], body="x", classification="SECRET"
        )
        with pytest.raises(ValueError):
            await svc.send(msg)
