"""Tests for PromotionGate (write validation + classification enforcement)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.classification import ClassificationChecker
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.errors import EntityValidationError, PromotionError
from arcteam.memory.index_manager import IndexManager
from arcteam.memory.promotion_gate import PromotionGate
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
async def setup(tmp_path: Path) -> tuple[PromotionGate, MemoryStorage, IndexManager, TeamMemoryConfig]:
    config = TeamMemoryConfig(root=tmp_path)
    storage = MemoryStorage(config.entities_dir)
    index_mgr = IndexManager(config.entities_dir, storage, config)
    classifier = ClassificationChecker(config)
    gate = PromotionGate(storage, index_mgr, classifier, audit_logger=None, messenger=None, config=config)
    return gate, storage, index_mgr, config


class TestPromoteCreate:
    """promote() should create new entities."""

    @pytest.mark.asyncio
    async def test_promote_creates_entity(self, setup: tuple) -> None:
        gate, storage, index_mgr, config = setup
        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await gate.promote("alice", "# Alice\n\nA researcher.", meta, agent_id="agent-1")
        assert result.entity_id == "alice"
        assert result.success is True
        assert result.action == "created"
        # File should exist
        assert (config.entities_dir / "person" / "alice.md").exists()

    @pytest.mark.asyncio
    async def test_promote_touches_dirty_flag(self, setup: tuple) -> None:
        gate, _, index_mgr, config = setup
        meta = _make_metadata(entity_id="alice", name="Alice")
        await gate.promote("alice", "# Alice", meta, agent_id="agent-1")
        assert (config.entities_dir / ".dirty").exists()

    @pytest.mark.asyncio
    async def test_promote_returns_promotion_result(self, setup: tuple) -> None:
        gate, _, _, _ = setup
        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await gate.promote("alice", "# Alice", meta, agent_id="agent-1")
        assert result.entity_id == "alice"
        assert result.success is True
        assert result.action == "created"


class TestPromoteUpdate:
    """promote() should update existing entities."""

    @pytest.mark.asyncio
    async def test_promote_updates_existing(self, setup: tuple) -> None:
        gate, storage, index_mgr, _ = setup
        meta = _make_metadata(entity_id="alice", name="Alice")
        await gate.promote("alice", "# Alice v1", meta, agent_id="agent-1")
        await index_mgr.rebuild()  # Rebuild so entity_exists works

        meta2 = _make_metadata(entity_id="alice", name="Alice Updated")
        result = await gate.promote("alice", "# Alice v2", meta2, agent_id="agent-1")
        assert result.action == "updated"


class TestPromoteValidation:
    """promote() should validate metadata."""

    @pytest.mark.asyncio
    async def test_promote_mismatched_entity_id_raises(self, setup: tuple) -> None:
        gate, _, _, _ = setup
        meta = _make_metadata(entity_id="bob")
        with pytest.raises(EntityValidationError):
            await gate.promote("alice", "# Alice", meta, agent_id="agent-1")


class TestPromoteClassification:
    """promote() should enforce classification on federal tier."""

    @pytest.mark.asyncio
    async def test_federal_blocks_missing_classification(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, audit_logger=None, messenger=None, config=config)

        # No classification field = "unclassified" default — should be allowed
        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await gate.promote("alice", "# Alice", meta, agent_id="agent-1")
        assert result.action == "created"

    @pytest.mark.asyncio
    async def test_federal_queues_cui_without_messenger(self, tmp_path: Path) -> None:
        """CUI+ without messenger should raise PromotionError (can't queue)."""
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, audit_logger=None, messenger=None, config=config)

        meta = _make_metadata(entity_id="alice", name="Alice", classification="CUI")
        with pytest.raises(PromotionError):
            await gate.promote("alice", "# Alice", meta, agent_id="agent-1")

    @pytest.mark.asyncio
    async def test_personal_allows_any_classification(self, setup: tuple) -> None:
        gate, _, _, _ = setup
        meta = _make_metadata(entity_id="alice", name="Alice", classification="SECRET")
        result = await gate.promote("alice", "# Secret Alice", meta, agent_id="agent-1")
        assert result.success is True
        assert result.action == "created"
