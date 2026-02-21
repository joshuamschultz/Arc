"""Tests for team memory types."""

from __future__ import annotations

import pytest

from arcteam.memory.types import (
    Classification,
    EntityFile,
    EntityMetadata,
    IndexEntry,
    MemoryStatus,
    PromotionResult,
    SearchResult,
)


class TestClassification:
    """Classification enum should follow US Government hierarchy."""

    def test_ordering(self) -> None:
        assert Classification.UNCLASSIFIED < Classification.CUI
        assert Classification.CUI < Classification.CONFIDENTIAL
        assert Classification.CONFIDENTIAL < Classification.SECRET
        assert Classification.SECRET < Classification.TOP_SECRET

    def test_values(self) -> None:
        assert Classification.UNCLASSIFIED == 0
        assert Classification.CUI == 1
        assert Classification.TOP_SECRET == 4

    def test_from_int(self) -> None:
        assert Classification(0) == Classification.UNCLASSIFIED
        assert Classification(3) == Classification.SECRET


class TestEntityMetadata:
    """EntityMetadata validates YAML frontmatter schema."""

    def test_minimal(self) -> None:
        meta = EntityMetadata(
            entity_type="person",
            entity_id="john-doe",
            name="John Doe",
            last_updated="2026-02-21",
        )
        assert meta.entity_type == "person"
        assert meta.status == "active"
        assert meta.classification == "unclassified"
        assert meta.links_to == []

    def test_full(self) -> None:
        meta = EntityMetadata(
            entity_type="organization",
            entity_id="nnsa",
            name="NNSA",
            status="active",
            last_updated="2026-02-21",
            last_verified="2026-02-21",
            created="2025-09-15",
            links_to=["DOE", "genesis-mission"],
            tags=["federal", "energy"],
            source_agents=["procurement-agent"],
            classification="cui",
        )
        assert meta.links_to == ["DOE", "genesis-mission"]
        assert meta.classification == "cui"

    def test_missing_required_raises(self) -> None:
        with pytest.raises(Exception):
            EntityMetadata(entity_type="person")  # type: ignore[call-arg]


class TestIndexEntry:
    """IndexEntry should include linked_from for backlink computation."""

    def test_defaults(self) -> None:
        entry = IndexEntry(
            entity_id="test",
            path="person/test.md",
            entity_type="person",
        )
        assert entry.linked_from == []
        assert entry.tags == []
        assert entry.status == "active"


class TestSearchResult:
    """SearchResult should carry score and hops."""

    def test_create(self) -> None:
        result = SearchResult(
            entity_id="nnsa",
            path="organization/nnsa.md",
            snippet="NNSA is a federal agency",
            score=2.5,
            hops=1,
            entity_type="organization",
        )
        assert result.score == 2.5
        assert result.hops == 1


class TestEntityFile:
    """EntityFile bundles metadata + content."""

    def test_create(self) -> None:
        meta = EntityMetadata(
            entity_type="person",
            entity_id="test",
            name="Test",
            last_updated="2026-02-21",
        )
        ef = EntityFile(metadata=meta, content="# Test\n\nSome content.")
        assert ef.content.startswith("# Test")


class TestPromotionResult:
    """PromotionResult tracks success/action."""

    def test_created(self) -> None:
        result = PromotionResult(success=True, entity_id="test", action="created")
        assert result.success is True

    def test_queued(self) -> None:
        result = PromotionResult(
            success=True,
            entity_id="test",
            action="queued_approval",
            message="CUI requires human approval",
        )
        assert result.action == "queued_approval"


class TestMemoryStatus:
    """MemoryStatus should have sensible defaults."""

    def test_defaults(self) -> None:
        status = MemoryStatus(enabled=True)
        assert status.entity_count == 0
        assert status.index_dirty is False
