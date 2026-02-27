"""Tests for TeamMemoryService (facade)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.memory.types import EntityMetadata, MemoryStatus


def _make_metadata(**overrides: object) -> EntityMetadata:
    defaults = {
        "entity_type": "person",
        "entity_id": "john-doe",
        "name": "John Doe",
        "last_updated": "2026-02-21",
    }
    defaults.update(overrides)
    return EntityMetadata(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def service(tmp_path: Path) -> TeamMemoryService:
    config = TeamMemoryConfig(root=tmp_path)
    return TeamMemoryService(config)


@pytest.fixture
def disabled_service(tmp_path: Path) -> TeamMemoryService:
    config = TeamMemoryConfig(root=tmp_path, enabled=False)
    return TeamMemoryService(config)


class TestServiceSearch:
    """search() should delegate to SearchEngine + classification filter."""

    @pytest.mark.asyncio
    async def test_search_empty(self, service: TeamMemoryService) -> None:
        results = await service.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_finds_promoted_entity(self, service: TeamMemoryService) -> None:
        meta = _make_metadata(entity_id="alice", name="Alice")
        await service.promote(
            "alice",
            "# Alice\n\nAlice is a nuclear physicist.",
            meta,
            agent_id="agent-1",
        )
        results = await service.search("nuclear physicist")
        assert len(results) > 0
        assert results[0].entity_id == "alice"


class TestServicePromote:
    """promote() should delegate to PromotionGate."""

    @pytest.mark.asyncio
    async def test_promote_creates_entity(self, service: TeamMemoryService) -> None:
        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await service.promote("alice", "# Alice", meta, agent_id="agent-1")
        assert result.success is True
        assert result.action == "created"

    @pytest.mark.asyncio
    async def test_promote_update(self, service: TeamMemoryService) -> None:
        meta = _make_metadata(entity_id="alice", name="Alice")
        await service.promote("alice", "# Alice v1", meta, agent_id="agent-1")
        # Force index rebuild so entity_exists sees it
        await service.rebuild_index()
        result = await service.promote("alice", "# Alice v2", meta, agent_id="agent-1")
        assert result.action == "updated"


class TestServiceGetEntity:
    """get_entity() should return entity by ID."""

    @pytest.mark.asyncio
    async def test_get_entity_found(self, service: TeamMemoryService) -> None:
        meta = _make_metadata(entity_id="alice", name="Alice")
        await service.promote("alice", "# Alice\n\nContent.", meta, agent_id="agent-1")
        await service.rebuild_index()
        entity = await service.get_entity("alice")
        assert entity is not None
        assert entity.metadata.entity_id == "alice"

    @pytest.mark.asyncio
    async def test_get_entity_missing(self, service: TeamMemoryService) -> None:
        entity = await service.get_entity("nonexistent")
        assert entity is None


class TestServiceListEntities:
    """list_entities() should return index entries."""

    @pytest.mark.asyncio
    async def test_list_entities_empty(self, service: TeamMemoryService) -> None:
        entries = await service.list_entities()
        assert entries == []

    @pytest.mark.asyncio
    async def test_list_entities(self, service: TeamMemoryService) -> None:
        for name in ("alice", "bob"):
            meta = _make_metadata(entity_id=name, name=name.title())
            await service.promote(name, f"# {name.title()}", meta, agent_id="agent-1")
        await service.rebuild_index()
        entries = await service.list_entities()
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_list_entities_by_type(self, service: TeamMemoryService) -> None:
        await service.promote(
            "alice",
            "# Alice",
            _make_metadata(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        await service.promote(
            "nnsa",
            "# NNSA",
            _make_metadata(entity_id="nnsa", name="NNSA", entity_type="organization"),
            agent_id="agent-1",
        )
        await service.rebuild_index()
        entries = await service.list_entities(entity_type="person")
        assert len(entries) == 1
        assert entries[0].entity_id == "alice"


class TestServiceStatus:
    """status() should return MemoryStatus."""

    @pytest.mark.asyncio
    async def test_status(self, service: TeamMemoryService) -> None:
        status = await service.status()
        assert isinstance(status, MemoryStatus)
        assert status.enabled is True
        assert status.entity_count == 0

    @pytest.mark.asyncio
    async def test_status_with_entities(self, service: TeamMemoryService) -> None:
        await service.promote(
            "alice",
            "# Alice",
            _make_metadata(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        await service.rebuild_index()
        status = await service.status()
        assert status.entity_count == 1


class TestNullObjectPattern:
    """When disabled, all methods return empty/no-op."""

    @pytest.mark.asyncio
    async def test_disabled_search(self, disabled_service: TeamMemoryService) -> None:
        results = await disabled_service.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_disabled_promote(self, disabled_service: TeamMemoryService) -> None:
        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await disabled_service.promote("alice", "# Alice", meta, agent_id="agent-1")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_disabled_get_entity(self, disabled_service: TeamMemoryService) -> None:
        entity = await disabled_service.get_entity("alice")
        assert entity is None

    @pytest.mark.asyncio
    async def test_disabled_list_entities(self, disabled_service: TeamMemoryService) -> None:
        entries = await disabled_service.list_entities()
        assert entries == []

    @pytest.mark.asyncio
    async def test_disabled_status(self, disabled_service: TeamMemoryService) -> None:
        status = await disabled_service.status()
        assert status.enabled is False
