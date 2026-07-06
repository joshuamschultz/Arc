"""Tests for C3 presence — typed EntityStatus + registry transitions (REQ-021)."""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityStatus, EntityType


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    return EntityRegistry(backend, audit)


def _agent(name: str) -> Entity:
    return Entity(
        did=f"did:arc:test:agent/{name}",
        handle=name,
        id=f"agent://{name}",
        name=name.title(),
        type=EntityType.AGENT,
    )


class TestEntityStatusEnum:
    """EntityStatus is a StrEnum with the five presence states."""

    def test_members(self) -> None:
        assert {s.value for s in EntityStatus} == {
            "active",
            "idle",
            "blocked",
            "waiting",
            "offline",
        }

    def test_is_str(self) -> None:
        assert EntityStatus.active == "active"


class TestEntityDefaultStatus:
    """A fresh Entity defaults to active presence."""

    def test_default_active(self) -> None:
        entity = _agent("a1")
        assert entity.status is EntityStatus.active


class TestSetStatus:
    """set_status transitions presence and audits it."""

    async def test_transition_persists(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1"))
        await registry.set_status("agent://a1", EntityStatus.idle)
        entity = await registry.get("agent://a1")
        assert entity is not None
        assert entity.status is EntityStatus.idle

    async def test_transition_audits(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1"))
        await registry.set_status("agent://a1", EntityStatus.offline)
        records = await registry._backend.read_stream("audit", "audit", after_seq=0)
        assert any(r["event_type"] == "entity.status_changed" for r in records)

    async def test_unknown_entity_raises(self, registry: EntityRegistry) -> None:
        with pytest.raises(ValueError, match="not found"):
            await registry.set_status("agent://ghost", EntityStatus.idle)


class TestUpdateStatusStillWorks:
    """The existing update_status entry point keeps working with the enum."""

    async def test_update_status(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1"))
        await registry.update_status("agent://a1", EntityStatus.waiting)
        entity = await registry.get("agent://a1")
        assert entity is not None
        assert entity.status is EntityStatus.waiting
