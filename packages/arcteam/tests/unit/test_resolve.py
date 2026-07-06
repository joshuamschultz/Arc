"""Tests for arcteam.registry.resolve — the sole address resolver (REQ-002)."""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry, UnknownHandle, resolve
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    reg = EntityRegistry(backend, audit)
    await reg.register(
        Entity(
            did="did:arc:test:agent/builder",
            handle="builder",
            id="agent://builder",
            name="Builder",
            type=EntityType.AGENT,
            roles=["dev"],
        )
    )
    await reg.register(
        Entity(
            did="did:arc:test:user/josh",
            handle="josh",
            id="user://josh",
            name="Josh",
            type=EntityType.USER,
            roles=["admin"],
        )
    )
    return reg


class TestResolveEntityRefs:
    """@handle, agent://, user://, bare handle, and did: all map to the DID."""

    async def test_at_handle_resolves_to_did(self, registry: EntityRegistry) -> None:
        assert await resolve(registry, "@builder") == "did:arc:test:agent/builder"

    async def test_agent_uri_resolves_to_did(self, registry: EntityRegistry) -> None:
        assert await resolve(registry, "agent://builder") == "did:arc:test:agent/builder"

    async def test_user_uri_resolves_to_did(self, registry: EntityRegistry) -> None:
        assert await resolve(registry, "user://josh") == "did:arc:test:user/josh"

    async def test_bare_handle_resolves_to_did(self, registry: EntityRegistry) -> None:
        assert await resolve(registry, "builder") == "did:arc:test:agent/builder"

    async def test_did_resolves_to_itself(self, registry: EntityRegistry) -> None:
        assert (
            await resolve(registry, "did:arc:test:agent/builder") == "did:arc:test:agent/builder"
        )


class TestResolveGroupRefs:
    """channel:// and role:// are group addresses, not identities — pass through."""

    async def test_channel_passthrough(self, registry: EntityRegistry) -> None:
        assert await resolve(registry, "channel://ops") == "channel://ops"

    async def test_role_passthrough(self, registry: EntityRegistry) -> None:
        assert await resolve(registry, "role://dev") == "role://dev"


class TestUnknownHandle:
    """Unknown entity refs raise UnknownHandle — never a silent DLQ (REQ-002)."""

    async def test_unknown_at_handle_raises(self, registry: EntityRegistry) -> None:
        with pytest.raises(UnknownHandle):
            await resolve(registry, "@ghost")

    async def test_unknown_agent_uri_raises(self, registry: EntityRegistry) -> None:
        with pytest.raises(UnknownHandle):
            await resolve(registry, "agent://ghost")

    async def test_unknown_did_raises(self, registry: EntityRegistry) -> None:
        with pytest.raises(UnknownHandle):
            await resolve(registry, "did:arc:test:agent/ghost")

    async def test_unknown_bare_handle_raises(self, registry: EntityRegistry) -> None:
        with pytest.raises(UnknownHandle):
            await resolve(registry, "ghost")
