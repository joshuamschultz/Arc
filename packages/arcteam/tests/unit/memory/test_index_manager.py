"""Tests for IndexManager (_index.json lifecycle)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.index_manager import IndexManager
from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import EntityMetadata


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
def setup(tmp_path: Path) -> tuple[IndexManager, MemoryStorage, TeamMemoryConfig]:
    config = TeamMemoryConfig(root=tmp_path)
    storage = MemoryStorage(config.entities_dir)
    mgr = IndexManager(config.entities_dir, storage, config)
    return mgr, storage, config


class TestIndexRebuild:
    """rebuild() should scan entity files and produce _index.json."""

    @pytest.mark.asyncio
    async def test_rebuild_empty(self, setup: tuple) -> None:
        mgr, _, config = setup
        index = await mgr.rebuild()
        assert index == {}
        assert config.index_path.exists()

    @pytest.mark.asyncio
    async def test_rebuild_with_entities(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        await storage.write_entity(
            "bob",
            _make_metadata(entity_id="bob", name="Bob", links_to=["alice"]),
            "# Bob",
        )
        index = await mgr.rebuild()
        assert "alice" in index
        assert "bob" in index
        assert index["bob"].links_to == ["alice"]
        # Backlinks computed
        assert "bob" in index["alice"].linked_from

    @pytest.mark.asyncio
    async def test_rebuild_sets_path(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        index = await mgr.rebuild()
        assert index["alice"].path == "person/alice.md"


class TestDirtyFlag:
    """Dirty flag should trigger rebuild on next get_index()."""

    @pytest.mark.asyncio
    async def test_touch_dirty_creates_marker(self, setup: tuple) -> None:
        mgr, _, config = setup
        await mgr.touch_dirty()
        assert (config.entities_dir / ".dirty").exists()

    @pytest.mark.asyncio
    async def test_get_index_rebuilds_when_dirty(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        await mgr.touch_dirty()
        index = await mgr.get_index()
        assert "alice" in index

    @pytest.mark.asyncio
    async def test_get_index_uses_cache_when_clean(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        await mgr.rebuild()
        # Add another entity but DON'T touch dirty
        await storage.write_entity("bob", _make_metadata(entity_id="bob", name="Bob"), "# Bob")
        index = await mgr.get_index()
        # Bob NOT in index because dirty wasn't touched
        assert "bob" not in index


class TestLookup:
    """lookup() should return IndexEntry or None."""

    @pytest.mark.asyncio
    async def test_lookup_found(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        await mgr.rebuild()
        entry = await mgr.lookup("alice")
        assert entry is not None
        assert entry.entity_id == "alice"

    @pytest.mark.asyncio
    async def test_lookup_missing(self, setup: tuple) -> None:
        mgr, _, _ = setup
        await mgr.rebuild()
        entry = await mgr.lookup("nonexistent")
        assert entry is None


class TestBacklinks:
    """get_backlinks() should find entities linking TO target."""

    @pytest.mark.asyncio
    async def test_backlinks(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        await storage.write_entity(
            "bob",
            _make_metadata(entity_id="bob", name="Bob", links_to=["alice"]),
            "# Bob",
        )
        await storage.write_entity(
            "charlie",
            _make_metadata(entity_id="charlie", name="Charlie", links_to=["alice", "bob"]),
            "# Charlie",
        )
        await mgr.rebuild()
        backlinks = await mgr.get_backlinks("alice")
        assert set(backlinks) == {"bob", "charlie"}

    @pytest.mark.asyncio
    async def test_backlinks_empty(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity("alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice")
        await mgr.rebuild()
        backlinks = await mgr.get_backlinks("alice")
        assert backlinks == []
