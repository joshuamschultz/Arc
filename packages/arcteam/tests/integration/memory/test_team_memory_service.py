"""Integration test: full TeamMemoryService lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.memory.types import Classification, EntityMetadata


def _meta(**overrides: object) -> EntityMetadata:
    defaults = {
        "entity_type": "person",
        "entity_id": "test",
        "name": "Test",
        "last_updated": "2026-02-21",
    }
    defaults.update(overrides)
    return EntityMetadata(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def service(tmp_path: Path) -> TeamMemoryService:
    config = TeamMemoryConfig(root=tmp_path)
    return TeamMemoryService(config)


@pytest.fixture
def federal_service(tmp_path: Path) -> TeamMemoryService:
    config = TeamMemoryConfig(root=tmp_path, tier="federal")
    return TeamMemoryService(config)


class TestFullLifecycle:
    """Full promote -> search -> get -> list cycle."""

    @pytest.mark.asyncio
    async def test_promote_and_search(self, service: TeamMemoryService) -> None:
        # Promote entities
        await service.promote(
            "alice",
            "# Alice\n\nAlice is a nuclear physicist at Los Alamos National Laboratory.",
            _meta(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        await service.promote(
            "bob",
            "# Bob\n\nBob is a software engineer in Denver.",
            _meta(entity_id="bob", name="Bob"),
            agent_id="agent-1",
        )
        # Force index rebuild
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()

        # Search finds relevant entity
        results = await service.search("nuclear physicist")
        assert len(results) > 0
        assert results[0].entity_id == "alice"

    @pytest.mark.asyncio
    async def test_promote_and_get_entity(self, service: TeamMemoryService) -> None:
        await service.promote(
            "alice",
            "# Alice\n\nDetailed content about Alice.",
            _meta(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()

        entity = await service.get_entity("alice")
        assert entity is not None
        assert entity.metadata.entity_id == "alice"
        assert "Detailed content" in entity.content

    @pytest.mark.asyncio
    async def test_list_entities(self, service: TeamMemoryService) -> None:
        await service.promote(
            "alice", "# Alice",
            _meta(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        await service.promote(
            "nnsa", "# NNSA",
            _meta(entity_id="nnsa", name="NNSA", entity_type="organization"),
            agent_id="agent-1",
        )
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()

        all_entries = await service.list_entities()
        assert len(all_entries) == 2

        people = await service.list_entities(entity_type="person")
        assert len(people) == 1
        assert people[0].entity_id == "alice"

    @pytest.mark.asyncio
    async def test_wiki_link_traversal(self, service: TeamMemoryService) -> None:
        # Create linked entities
        await service.promote(
            "alice",
            "# Alice\n\nAlice studies fusion energy at [[los-alamos]].",
            _meta(entity_id="alice", name="Alice", links_to=["los-alamos"]),
            agent_id="agent-1",
        )
        await service.promote(
            "los-alamos",
            "# Los Alamos\n\nNational nuclear laboratory for fusion research.",
            _meta(entity_id="los-alamos", name="Los Alamos", entity_type="organization"),
            agent_id="agent-1",
        )
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()

        results = await service.search("fusion energy")
        entity_ids = [r.entity_id for r in results]
        assert "alice" in entity_ids
        # Los Alamos should also appear (via traversal or direct BM25 match)
        assert "los-alamos" in entity_ids

    @pytest.mark.asyncio
    async def test_update_entity(self, service: TeamMemoryService) -> None:
        await service.promote(
            "alice", "# Alice v1\n\nOriginal.",
            _meta(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()

        result = await service.promote(
            "alice", "# Alice v2\n\nUpdated content.",
            _meta(entity_id="alice", name="Alice Updated"),
            agent_id="agent-1",
        )
        assert result.action == "updated"
        await service._index_mgr.rebuild()

        entity = await service.get_entity("alice")
        assert entity is not None
        assert "Updated content" in entity.content


class TestClassificationFiltering:
    """Classification should silently filter results."""

    @pytest.mark.asyncio
    async def test_federal_filters_search(self, federal_service: TeamMemoryService) -> None:
        await federal_service.promote(
            "public-doc", "# Public\n\nPublic research about fusion.",
            _meta(entity_id="public-doc", name="Public", classification="unclassified"),
            agent_id="agent-1",
        )
        # CUI entity would need approval queue — skip for this test
        assert federal_service._index_mgr is not None
        await federal_service._index_mgr.rebuild()

        # UNCLASSIFIED agent sees UNCLASSIFIED entity
        results = await federal_service.search(
            "fusion", agent_classification=Classification.UNCLASSIFIED
        )
        entity_ids = [r.entity_id for r in results]
        assert "public-doc" in entity_ids

    @pytest.mark.asyncio
    async def test_federal_blocks_get_entity(self, federal_service: TeamMemoryService) -> None:
        # Directly write a SECRET entity (bypass gate for test)
        assert federal_service._storage is not None
        assert federal_service._index_mgr is not None
        await federal_service._storage.write_entity(
            "secret-doc",
            _meta(entity_id="secret-doc", name="Secret", classification="SECRET"),
            "# Secret document",
        )
        await federal_service._index_mgr.rebuild()

        # UNCLASSIFIED agent should not see SECRET entity
        entity = await federal_service.get_entity(
            "secret-doc", agent_classification=Classification.UNCLASSIFIED
        )
        assert entity is None

        # SECRET agent should see it
        entity = await federal_service.get_entity(
            "secret-doc", agent_classification=Classification.SECRET
        )
        assert entity is not None


class TestNullObjectPattern:
    """Disabled service returns empty for everything."""

    @pytest.mark.asyncio
    async def test_disabled_full_lifecycle(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, enabled=False)
        svc = TeamMemoryService(config)

        result = await svc.promote(
            "alice", "# Alice",
            _meta(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        assert result.success is False

        results = await svc.search("anything")
        assert results == []

        entity = await svc.get_entity("alice")
        assert entity is None

        entries = await svc.list_entities()
        assert entries == []

        status = await svc.status()
        assert status.enabled is False


class TestIndexRebuild:
    """Index rebuild produces correct manifest."""

    @pytest.mark.asyncio
    async def test_rebuild_after_promotes(self, service: TeamMemoryService) -> None:
        for i in range(5):
            await service.promote(
                f"person-{i}", f"# Person {i}\n\nContent for person {i}.",
                _meta(entity_id=f"person-{i}", name=f"Person {i}"),
                agent_id="agent-1",
            )
        assert service._index_mgr is not None
        index = await service._index_mgr.rebuild()
        assert len(index) == 5
        for i in range(5):
            assert f"person-{i}" in index

    @pytest.mark.asyncio
    async def test_index_backlinks(self, service: TeamMemoryService) -> None:
        await service.promote(
            "alice", "# Alice",
            _meta(entity_id="alice", name="Alice", links_to=["bob"]),
            agent_id="agent-1",
        )
        await service.promote(
            "bob", "# Bob",
            _meta(entity_id="bob", name="Bob"),
            agent_id="agent-1",
        )
        assert service._index_mgr is not None
        index = await service._index_mgr.rebuild()
        assert "alice" in index["bob"].linked_from
