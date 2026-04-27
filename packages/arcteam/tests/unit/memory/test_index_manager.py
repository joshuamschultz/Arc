"""Tests for IndexManager (_index.json lifecycle)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.errors import IndexCorruptionError
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
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
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
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
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
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
        await mgr.touch_dirty()
        index = await mgr.get_index()
        assert "alice" in index

    @pytest.mark.asyncio
    async def test_get_index_uses_cache_when_clean(self, setup: tuple) -> None:
        mgr, storage, _ = setup
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
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
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
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
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
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
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
        await mgr.rebuild()
        backlinks = await mgr.get_backlinks("alice")
        assert backlinks == []


# ---------------------------------------------------------------------------
# SHA-256 integrity check — universal tier enforcement (ADR-019)
# ---------------------------------------------------------------------------


class TestChecksumPersonalTierTamper:
    """Personal tier: if checksum file exists and content was tampered, raise IndexCorruptionError.

    ADR-019 four-pillars-universal: tamper-evident integrity runs at ALL tiers.
    Tier-stringency: missing checksum file = warning (personal) vs hard error (enterprise/federal).
    But when the checksum file IS present, it MUST validate at every tier.
    """

    @pytest.mark.asyncio
    async def test_personal_tier_tampered_index_raises(self, tmp_path: Path) -> None:
        """Personal tier: tampered _index.json raises IndexCorruptionError when .sha256 exists."""
        config = TeamMemoryConfig(root=tmp_path, tier="personal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)

        # Write an entity so the index has content
        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
        # Build the index (writes _index.json + _index.sha256)
        await mgr.rebuild()

        assert config.index_path.exists(), "_index.json must exist after rebuild"
        checksum_path = config.entities_dir / "_index.sha256"
        assert checksum_path.exists(), "_index.sha256 must exist after rebuild"

        # Tamper the index content (checksum file still holds the original hash)
        original = config.index_path.read_text(encoding="utf-8")
        config.index_path.write_text(original + "\n# TAMPERED", encoding="utf-8")

        # Clear cache so next get_index() reads from disk
        mgr._cache = None  # type: ignore[attr-defined]  # forced reset for test isolation

        with pytest.raises(IndexCorruptionError):
            await mgr.get_index()

    @pytest.mark.asyncio
    async def test_personal_tier_missing_checksum_is_warning_not_error(
        self, tmp_path: Path
    ) -> None:
        """Personal tier: missing checksum file logs a warning but does NOT raise.

        Developer may have manually edited the index. ADR-019 tier-stringency knob:
        missing = warn (personal) | hard error (enterprise, federal).
        """
        config = TeamMemoryConfig(root=tmp_path, tier="personal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)

        await storage.write_entity(
            "alice", _make_metadata(entity_id="alice", name="Alice"), "# Alice"
        )
        await mgr.rebuild()

        # Remove the checksum file
        checksum_path = config.entities_dir / "_index.sha256"
        checksum_path.unlink()

        mgr._cache = None  # type: ignore[attr-defined]

        # Should load without error at personal tier (missing checksum = warn only)
        index = await mgr.get_index()
        assert "alice" in index


class TestChecksumEnterpriseTierTamper:
    """Enterprise tier: tampered index raises IndexCorruptionError when .sha256 exists.

    ADR-019: enterprise tier = hard error on missing checksum (same as federal).
    """

    @pytest.mark.asyncio
    async def test_enterprise_tier_tampered_index_raises(self, tmp_path: Path) -> None:
        """Enterprise tier: tampered _index.json raises IndexCorruptionError."""
        config = TeamMemoryConfig(root=tmp_path, tier="enterprise")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)

        await storage.write_entity(
            "bob", _make_metadata(entity_id="bob", name="Bob"), "# Bob"
        )
        await mgr.rebuild()

        checksum_path = config.entities_dir / "_index.sha256"
        assert checksum_path.exists()

        original = config.index_path.read_text(encoding="utf-8")
        config.index_path.write_text(original + "\n# TAMPERED", encoding="utf-8")

        mgr._cache = None  # type: ignore[attr-defined]

        with pytest.raises(IndexCorruptionError):
            await mgr.get_index()

    @pytest.mark.asyncio
    async def test_enterprise_tier_missing_checksum_raises(self, tmp_path: Path) -> None:
        """Enterprise tier: missing checksum file is a hard error (unlike personal)."""
        config = TeamMemoryConfig(root=tmp_path, tier="enterprise")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)

        await storage.write_entity(
            "bob", _make_metadata(entity_id="bob", name="Bob"), "# Bob"
        )
        await mgr.rebuild()

        checksum_path = config.entities_dir / "_index.sha256"
        checksum_path.unlink()

        mgr._cache = None  # type: ignore[attr-defined]

        with pytest.raises(IndexCorruptionError):
            await mgr.get_index()


class TestChecksumFederalTierPreserved:
    """Federal tier behavior is preserved: hard error on tamper and missing checksum."""

    @pytest.mark.asyncio
    async def test_federal_tier_tampered_raises(self, tmp_path: Path) -> None:
        """Federal tier: tampered index raises IndexCorruptionError."""
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)

        await storage.write_entity(
            "charlie", _make_metadata(entity_id="charlie", name="Charlie"), "# Charlie"
        )
        await mgr.rebuild()

        original = config.index_path.read_text(encoding="utf-8")
        config.index_path.write_text(original + "\n# TAMPERED", encoding="utf-8")

        mgr._cache = None  # type: ignore[attr-defined]

        with pytest.raises(IndexCorruptionError):
            await mgr.get_index()

    @pytest.mark.asyncio
    async def test_federal_tier_missing_checksum_raises(self, tmp_path: Path) -> None:
        """Federal tier: missing checksum file is a hard error."""
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)

        await storage.write_entity(
            "charlie", _make_metadata(entity_id="charlie", name="Charlie"), "# Charlie"
        )
        await mgr.rebuild()

        checksum_path = config.entities_dir / "_index.sha256"
        checksum_path.unlink()

        mgr._cache = None  # type: ignore[attr-defined]

        with pytest.raises(IndexCorruptionError):
            await mgr.get_index()
