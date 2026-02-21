"""Integration test: concurrency behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.memory.types import EntityMetadata


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


class TestConcurrentPromotes:
    """Concurrent promotes should not corrupt data."""

    @pytest.mark.asyncio
    async def test_concurrent_different_entities(self, service: TeamMemoryService) -> None:
        """Concurrent promotes to different entities should all succeed."""
        tasks = []
        for i in range(10):
            eid = f"person-{i}"
            tasks.append(
                service.promote(
                    eid,
                    f"# Person {i}\n\nContent for person {i}.",
                    _meta(entity_id=eid, name=f"Person {i}"),
                    agent_id=f"agent-{i}",
                )
            )
        results = await asyncio.gather(*tasks)
        assert all(r.success for r in results)

        # Rebuild and verify all entities exist
        assert service._index_mgr is not None
        index = await service._index_mgr.rebuild()
        assert len(index) == 10

    @pytest.mark.asyncio
    async def test_concurrent_same_entity(self, service: TeamMemoryService) -> None:
        """Concurrent promotes to same entity should serialize via flock."""
        tasks = []
        for i in range(5):
            tasks.append(
                service.promote(
                    "shared",
                    f"# Shared v{i}\n\nVersion {i}.",
                    _meta(entity_id="shared", name="Shared"),
                    agent_id=f"agent-{i}",
                )
            )
        results = await asyncio.gather(*tasks)
        assert all(r.success for r in results)

        # Entity should exist (last writer wins)
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()
        entity = await service.get_entity("shared")
        assert entity is not None


class TestConcurrentSearchAndPromote:
    """Concurrent search + promote should not error."""

    @pytest.mark.asyncio
    async def test_search_during_promote(self, service: TeamMemoryService) -> None:
        # Pre-populate
        await service.promote(
            "alice", "# Alice\n\nResearcher in physics.",
            _meta(entity_id="alice", name="Alice"),
            agent_id="agent-1",
        )
        assert service._index_mgr is not None
        await service._index_mgr.rebuild()

        # Concurrent search + promote
        async def do_search() -> list:
            return await service.search("physics")

        async def do_promote() -> object:
            return await service.promote(
                "bob", "# Bob\n\nAlso researches physics.",
                _meta(entity_id="bob", name="Bob"),
                agent_id="agent-2",
            )

        results = await asyncio.gather(
            do_search(), do_promote(), do_search(), do_search()
        )
        # No exceptions = pass
        assert len(results) == 4


class TestConcurrentIndexRebuild:
    """Concurrent index rebuilds should not corrupt _index.json."""

    @pytest.mark.asyncio
    async def test_concurrent_rebuilds(self, service: TeamMemoryService) -> None:
        # Pre-populate
        for i in range(5):
            await service.promote(
                f"p-{i}", f"# Person {i}",
                _meta(entity_id=f"p-{i}", name=f"Person {i}"),
                agent_id="agent-1",
            )

        assert service._index_mgr is not None
        # Concurrent rebuilds
        results = await asyncio.gather(
            service._index_mgr.rebuild(),
            service._index_mgr.rebuild(),
            service._index_mgr.rebuild(),
        )
        # All should produce valid indexes with 5 entities
        for index in results:
            assert len(index) == 5
