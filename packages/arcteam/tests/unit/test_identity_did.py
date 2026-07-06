"""Tests for DID-keyed identity spine (REQ-001, REQ-003).

Entity carries a required cryptographic DID + unique handle; the registry
is keyed by DID; and a freshly-registered agent is immediately addressable
by handle/URI — the `sender_unauthorized` DLQ bug is gone.
"""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Channel, Entity, EntityType, Message


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    return EntityRegistry(backend, audit)


def _agent(handle: str) -> Entity:
    return Entity(
        did=f"did:arc:local:executor/{handle}",
        handle=handle,
        id=f"agent://{handle}",
        name=handle.title(),
        type=EntityType.AGENT,
        roles=["executor"],
    )


class TestEntityRequiresDid:
    """did and handle are required fields (REQ-001, fail-closed)."""

    def test_missing_did_rejected(self) -> None:
        with pytest.raises(ValueError):
            Entity(handle="x", id="agent://x", name="X", type=EntityType.AGENT)  # type: ignore[call-arg]

    def test_missing_handle_rejected(self) -> None:
        with pytest.raises(ValueError):
            Entity(  # type: ignore[call-arg]
                did="did:arc:local:executor/x",
                id="agent://x",
                name="X",
                type=EntityType.AGENT,
            )


class TestRegistryKeyedByDid:
    """Storage is keyed by DID; lookups work by handle, URI, and DID."""

    async def test_get_by_did(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("researcher"))
        got = await registry.get("did:arc:local:executor/researcher")
        assert got is not None
        assert got.handle == "researcher"

    async def test_get_by_handle_and_uri(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("researcher"))
        by_uri = await registry.get("agent://researcher")
        by_at = await registry.get("@researcher")
        assert by_uri is not None and by_at is not None
        assert by_uri.did == by_at.did == "did:arc:local:executor/researcher"

    async def test_duplicate_handle_rejected(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("researcher"))
        clash = Entity(
            did="did:arc:local:executor/other",
            handle="researcher",
            id="agent://researcher",
            name="Other",
            type=EntityType.AGENT,
        )
        with pytest.raises(ValueError, match="Handle already registered"):
            await registry.register(clash)


class TestFreshAgentImmediatelyAddressable:
    """REQ-003: the `sender_unauthorized` DLQ bug is fixed.

    A just-registered agent can send immediately — no DLQ miss.
    """

    async def test_fresh_agent_can_send(self, registry: EntityRegistry) -> None:
        audit = registry._audit
        backend = registry._backend
        svc = MessagingService(backend, registry, audit)
        await registry.register(_agent("researcher"))
        await svc.create_channel(Channel(name="ops", members=["agent://researcher"]))

        sent = await svc.send(
            Message(sender="agent://researcher", to=["channel://ops"], body="hello")
        )
        assert sent.seq == 1

        dlq = await svc.dlq_list()
        assert not any(d["meta"].get("dlq_reason") == "sender_unauthorized" for d in dlq)
