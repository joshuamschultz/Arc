"""Tests for security fixes: path traversal, entity validation, classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcteam.memory.classification import ClassificationChecker
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.errors import EntityValidationError
from arcteam.memory.index_manager import IndexManager
from arcteam.memory.promotion_gate import PromotionGate
from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import Classification, EntityMetadata


def _make_metadata(**overrides: object) -> EntityMetadata:
    defaults = {
        "entity_type": "person",
        "entity_id": "john-doe",
        "name": "John Doe",
        "last_updated": "2026-02-21",
    }
    defaults.update(overrides)
    return EntityMetadata(**defaults)  # type: ignore[arg-type]


class TestPathTraversal:
    """V-001: Path traversal via entity_type/entity_id must be blocked."""

    def test_path_component_rejects_slashes(self) -> None:
        with pytest.raises(EntityValidationError, match="invalid characters"):
            MemoryStorage.validate_path_component("../../etc/passwd", "entity_id")

    def test_path_component_rejects_dotdot(self) -> None:
        with pytest.raises(EntityValidationError, match="invalid characters"):
            MemoryStorage.validate_path_component("..", "entity_id")

    def test_path_component_rejects_empty(self) -> None:
        with pytest.raises(EntityValidationError, match="must not be empty"):
            MemoryStorage.validate_path_component("", "entity_id")

    def test_path_component_allows_valid_names(self) -> None:
        # Should not raise
        MemoryStorage.validate_path_component("alice-doe", "entity_id")
        MemoryStorage.validate_path_component("person_type", "entity_type")
        MemoryStorage.validate_path_component("org.name", "entity_type")
        MemoryStorage.validate_path_component("user123", "entity_id")

    def test_path_component_rejects_leading_dot(self) -> None:
        with pytest.raises(EntityValidationError, match="invalid characters"):
            MemoryStorage.validate_path_component(".hidden", "entity_id")

    @pytest.mark.asyncio
    async def test_promote_rejects_path_traversal_entity_id(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path)
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        meta = _make_metadata(entity_id="../../etc/passwd")
        with pytest.raises(EntityValidationError, match="invalid characters"):
            await gate.promote("../../etc/passwd", "# Evil", meta, "agent-1")

    @pytest.mark.asyncio
    async def test_promote_rejects_path_traversal_entity_type(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, entity_types=["../../etc"])
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        meta = _make_metadata(entity_id="test", entity_type="../../etc")
        with pytest.raises(EntityValidationError, match="invalid characters"):
            await gate.promote("test", "# Evil", meta, "agent-1")


class TestEntityTypeAllowlist:
    """V-005: entity_type must be in configured allowlist."""

    @pytest.mark.asyncio
    async def test_rejects_unknown_entity_type(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path)
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        meta = _make_metadata(entity_id="test", entity_type="malicious")
        with pytest.raises(EntityValidationError, match="not in allowed types"):
            await gate.promote("test", "# Test", meta, "agent-1")

    @pytest.mark.asyncio
    async def test_accepts_configured_entity_type(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path)
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        for etype in config.entity_types:
            meta = _make_metadata(entity_id=f"test-{etype}", entity_type=etype, name="Test")
            result = await gate.promote(f"test-{etype}", "# Test", meta, "agent-1")
            assert result.success is True


class TestClassificationEnforcement:
    """PG-10: Federal tier must block promotions without classification."""

    @pytest.mark.asyncio
    async def test_federal_allows_explicit_classification(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        # Default classification is "unclassified" which is truthy
        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await gate.promote("alice", "# Alice", meta, "agent-1")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_federal_blocks_empty_classification(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        meta = _make_metadata(entity_id="alice", name="Alice", classification="")
        with pytest.raises(EntityValidationError, match="classification is required"):
            await gate.promote("alice", "# Alice", meta, "agent-1")

    @pytest.mark.asyncio
    async def test_personal_allows_empty_classification(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, tier="personal")
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        meta = _make_metadata(entity_id="alice", name="Alice", classification="")
        result = await gate.promote("alice", "# Alice", meta, "agent-1")
        assert result.success is True


class TestTokenBudget:
    """EG-6: Token budget enforcement on promote."""

    @pytest.mark.asyncio
    async def test_rejects_oversized_content(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, per_entity_budget=10)
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        # 100 words >> 10 token budget
        big_content = " ".join(["word"] * 100)
        meta = _make_metadata(entity_id="alice", name="Alice")
        with pytest.raises(EntityValidationError, match="exceeds token budget"):
            await gate.promote("alice", big_content, meta, "agent-1")

    @pytest.mark.asyncio
    async def test_accepts_within_budget(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, per_entity_budget=800)
        storage = MemoryStorage(config.entities_dir)
        index_mgr = IndexManager(config.entities_dir, storage, config)
        classifier = ClassificationChecker(config)
        gate = PromotionGate(storage, index_mgr, classifier, None, None, config)

        meta = _make_metadata(entity_id="alice", name="Alice")
        result = await gate.promote("alice", "# Alice\n\nShort content.", meta, "agent-1")
        assert result.success is True


class TestUnknownClassificationWarning:
    """Unknown classification values should warn."""

    def test_unknown_value_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="arcteam.memory.classification"):
            result = ClassificationChecker.parse_classification("SECERT")
        assert result == Classification.UNCLASSIFIED
        assert "Unknown classification value" in caplog.text

    def test_valid_values_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="arcteam.memory.classification"):
            ClassificationChecker.parse_classification("SECRET")
        assert "Unknown" not in caplog.text


class TestIndexChecksum:
    """IX-7: SHA-256 checksum on index for integrity verification."""

    @pytest.mark.asyncio
    async def test_rebuild_creates_checksum_file(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)
        meta = _make_metadata(entity_id="alice", name="Alice")
        await storage.write_entity("alice", meta, "# Alice")
        await mgr.rebuild()
        assert (config.entities_dir / "_index.sha256").exists()

    @pytest.mark.asyncio
    async def test_federal_detects_corrupted_index(self, tmp_path: Path) -> None:
        """Federal tier: corrupted index raises IndexCorruptionError — does NOT silently rebuild.

        ADR-019 four-pillars-universal: silently rebuilding over a tampered index is not
        safe. The previous behavior (rebuild on corruption) masked tamper events.
        Now get_index() propagates IndexCorruptionError so callers can act.
        """
        from arcteam.memory.errors import IndexCorruptionError

        config = TeamMemoryConfig(root=tmp_path, tier="federal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)
        meta = _make_metadata(entity_id="alice", name="Alice")
        await storage.write_entity("alice", meta, "# Alice")
        await mgr.rebuild()

        # Tamper with the index file
        index_path = config.entities_dir / "_index.json"
        content = index_path.read_text()
        index_path.write_text(content.replace("alice", "mallory"))

        # Loading must raise — not silently rebuild over tampered data
        mgr2 = IndexManager(config.entities_dir, storage, config)
        with pytest.raises(IndexCorruptionError):
            await mgr2.get_index()

    @pytest.mark.asyncio
    async def test_personal_checksum_present_and_invalid_raises(self, tmp_path: Path) -> None:
        """Personal tier: if _index.sha256 is present but invalid, raises IndexCorruptionError.

        ADR-019 four-pillars-universal: when a checksum file IS present at any tier,
        it MUST validate. Personal-tier missing checksum = warning (developer may edit manually),
        but a present-but-garbage checksum = clear tamper signal → hard error at every tier.

        Previously named 'test_personal_skips_checksum' and asserted no error was raised.
        Flipped: personal tier no longer bypasses checksum validation when the file exists.
        """
        from arcteam.memory.errors import IndexCorruptionError

        config = TeamMemoryConfig(root=tmp_path, tier="personal")
        storage = MemoryStorage(config.entities_dir)
        mgr = IndexManager(config.entities_dir, storage, config)
        meta = _make_metadata(entity_id="alice", name="Alice")
        await storage.write_entity("alice", meta, "# Alice")
        await mgr.rebuild()

        # Replace valid checksum with garbage — tamper signal
        checksum_path = config.entities_dir / "_index.sha256"
        assert checksum_path.exists(), "rebuild() must write checksum"
        checksum_path.write_text("garbage")

        mgr2 = IndexManager(config.entities_dir, storage, config)
        with pytest.raises(IndexCorruptionError):
            await mgr2.get_index()


class TestDecisionsStorage:
    """DS-1: Decisions append-only JSONL storage."""

    @pytest.mark.asyncio
    async def test_record_decision_creates_jsonl(self, tmp_path: Path) -> None:
        from arcteam.memory.service import TeamMemoryService

        config = TeamMemoryConfig(root=tmp_path)
        service = TeamMemoryService(config)
        await service.record_decision(
            {"title": "Use BM25Plus", "rationale": "Better for small corpora"},
            agent_id="agent-1",
        )
        decisions_path = tmp_path / "decisions.jsonl"
        assert decisions_path.exists()
        import json

        line = decisions_path.read_text().strip()
        record = json.loads(line)
        assert record["title"] == "Use BM25Plus"
        assert record["agent_id"] == "agent-1"
        assert "timestamp" in record

    @pytest.mark.asyncio
    async def test_record_multiple_decisions_appends(self, tmp_path: Path) -> None:
        from arcteam.memory.service import TeamMemoryService

        config = TeamMemoryConfig(root=tmp_path)
        service = TeamMemoryService(config)
        for i in range(3):
            await service.record_decision({"title": f"Decision {i}"}, agent_id="agent-1")
        decisions_path = tmp_path / "decisions.jsonl"
        lines = decisions_path.read_text().strip().split("\n")
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_disabled_service_skips_decisions(self, tmp_path: Path) -> None:
        from arcteam.memory.service import TeamMemoryService

        config = TeamMemoryConfig(root=tmp_path, enabled=False)
        service = TeamMemoryService(config)
        await service.record_decision({"title": "Nope"}, agent_id="agent-1")
        decisions_path = tmp_path / "decisions.jsonl"
        assert not decisions_path.exists()


class TestPublicAccessors:
    """Encapsulation fixes: verify public accessors work."""

    def test_storage_entities_dir_property(self, tmp_path: Path) -> None:
        storage = MemoryStorage(tmp_path)
        assert storage.entities_dir == tmp_path

    @pytest.mark.asyncio
    async def test_service_rebuild_index(self, tmp_path: Path) -> None:
        from arcteam.memory.service import TeamMemoryService

        config = TeamMemoryConfig(root=tmp_path)
        service = TeamMemoryService(config)
        meta = _make_metadata(entity_id="alice", name="Alice")
        await service.promote("alice", "# Alice", meta, agent_id="agent-1")
        index = await service.rebuild_index()
        assert "alice" in index

    @pytest.mark.asyncio
    async def test_disabled_service_rebuild_returns_empty(self, tmp_path: Path) -> None:
        from arcteam.memory.service import TeamMemoryService

        config = TeamMemoryConfig(root=tmp_path, enabled=False)
        service = TeamMemoryService(config)
        index = await service.rebuild_index()
        assert index == {}
