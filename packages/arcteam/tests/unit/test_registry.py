"""Tests for arcteam.registry — EntityRegistry with role-based queries."""

from __future__ import annotations

import pytest

from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, hmac_key=b"test-key")
    await audit.initialize()
    return EntityRegistry(backend, audit)


def _agent(name: str, roles: list[str] | None = None) -> Entity:
    return Entity(
        id=f"agent://{name}",
        name=name.title(),
        type=EntityType.AGENT,
        roles=roles or [],
    )


def _user(name: str, roles: list[str] | None = None) -> Entity:
    return Entity(
        id=f"user://{name}",
        name=name.title(),
        type=EntityType.USER,
        roles=roles or [],
    )


class TestRegisterAndGet:
    """Register agent, get it back."""

    async def test_register_and_get(self, registry: EntityRegistry) -> None:
        agent = _agent("a1", roles=["ops"])
        await registry.register(agent)
        result = await registry.get("agent://a1")
        assert result is not None
        assert result.id == "agent://a1"
        assert result.name == "A1"
        assert result.roles == ["ops"]
        assert result.created != ""  # Auto-set


class TestRejectDuplicate:
    """Reject duplicate ID."""

    async def test_duplicate_raises(self, registry: EntityRegistry) -> None:
        agent = _agent("a1")
        await registry.register(agent)
        with pytest.raises(ValueError, match="already registered"):
            await registry.register(agent)


class TestFilterByRole:
    """Filter by role."""

    async def test_by_role(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1", roles=["ops", "dev"]))
        await registry.register(_agent("a2", roles=["ops"]))
        await registry.register(_agent("a3", roles=["dev"]))
        await registry.register(_user("u1", roles=["admin"]))

        ops = await registry.by_role("ops")
        assert len(ops) == 2
        assert {e.id for e in ops} == {"agent://a1", "agent://a2"}

        dev = await registry.list_entities(role="dev")
        assert len(dev) == 2

    async def test_no_matching_role(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1", roles=["ops"]))
        result = await registry.by_role("nonexistent")
        assert result == []


class TestAuditRecords:
    """All operations generate audit records."""

    async def test_register_generates_audit(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1", roles=["ops"]))
        # Check audit stream has records
        records = await registry._backend.read_stream("audit", "audit", after_seq=0)
        assert len(records) >= 1
        assert records[0]["event_type"] == "entity.registered"

    async def test_status_change_generates_audit(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1"))
        await registry.update_status("agent://a1", "inactive")
        records = await registry._backend.read_stream("audit", "audit", after_seq=0)
        assert any(r["event_type"] == "entity.status_changed" for r in records)


class TestUpdateStatus:
    """Update status."""

    async def test_update_status(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1"))
        await registry.update_status("agent://a1", "inactive")
        entity = await registry.get("agent://a1")
        assert entity is not None
        assert entity.status == "inactive"

    async def test_update_status_not_found(self, registry: EntityRegistry) -> None:
        with pytest.raises(ValueError, match="not found"):
            await registry.update_status("agent://missing", "inactive")


class TestUpdateEntity:
    """SPEC-019 T1.2: write-through entity updates with audit emission."""

    async def test_update_writes_through(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1", roles=["ops"]))
        entity = await registry.get("agent://a1")
        assert entity is not None
        entity.workspace_path = "/abs/path/agent_a1"
        await registry.update(entity)
        readback = await registry.get("agent://a1")
        assert readback is not None
        assert readback.workspace_path == "/abs/path/agent_a1"
        # Other fields preserved
        assert readback.roles == ["ops"]

    async def test_update_emits_audit(self, registry: EntityRegistry) -> None:
        await registry.register(_agent("a1"))
        entity = await registry.get("agent://a1")
        assert entity is not None
        entity.workspace_path = "/abs/x"
        await registry.update(entity)
        records = await registry._backend.read_stream("audit", "audit", after_seq=0)
        assert any(r["event_type"] == "entity.updated" for r in records)

    async def test_update_unknown_entity_raises(self, registry: EntityRegistry) -> None:
        ghost = _agent("ghost")
        with pytest.raises(ValueError, match="not found"):
            await registry.update(ghost)
