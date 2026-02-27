"""Tests for ClassificationChecker (access control enforcement)."""

from __future__ import annotations

from arcteam.memory.classification import ClassificationChecker
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.types import Classification, IndexEntry, SearchResult


class TestParseClassification:
    """parse_classification should handle all valid strings."""

    def test_parse_unclassified(self) -> None:
        assert (
            ClassificationChecker.parse_classification("unclassified")
            == Classification.UNCLASSIFIED
        )

    def test_parse_cui(self) -> None:
        assert ClassificationChecker.parse_classification("CUI") == Classification.CUI

    def test_parse_secret(self) -> None:
        assert ClassificationChecker.parse_classification("SECRET") == Classification.SECRET

    def test_parse_top_secret(self) -> None:
        assert (
            ClassificationChecker.parse_classification("top_secret") == Classification.TOP_SECRET
        )

    def test_parse_unknown_defaults_unclassified(self) -> None:
        assert ClassificationChecker.parse_classification("garbage") == Classification.UNCLASSIFIED

    def test_parse_empty_defaults_unclassified(self) -> None:
        assert ClassificationChecker.parse_classification("") == Classification.UNCLASSIFIED


class TestCheckAccessPersonal:
    """Personal tier: no enforcement, always allows."""

    def test_personal_allows_all(self) -> None:
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        assert checker.check_access("SECRET", Classification.UNCLASSIFIED) is True


class TestCheckAccessFederal:
    """Federal tier: hard block on clearance violation."""

    def test_federal_allows_same_level(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        checker = ClassificationChecker(config)
        assert checker.check_access("SECRET", Classification.SECRET) is True

    def test_federal_allows_higher_clearance(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        checker = ClassificationChecker(config)
        assert checker.check_access("CUI", Classification.SECRET) is True

    def test_federal_blocks_lower_clearance(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        checker = ClassificationChecker(config)
        assert checker.check_access("SECRET", Classification.CUI) is False

    def test_federal_allows_unclassified(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        checker = ClassificationChecker(config)
        assert checker.check_access("unclassified", Classification.UNCLASSIFIED) is True


class TestCheckAccessEnterprise:
    """Enterprise tier: warn + block (same enforcement as federal)."""

    def test_enterprise_blocks_lower_clearance(self) -> None:
        config = TeamMemoryConfig(tier="enterprise")
        checker = ClassificationChecker(config)
        assert checker.check_access("SECRET", Classification.CUI) is False


class TestFilterResults:
    """filter_results should silently remove entities above clearance."""

    def test_filter_search_results(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        checker = ClassificationChecker(config)
        results = [
            SearchResult(
                entity_id="a",
                path="person/a.md",
                snippet="",
                score=1.0,
                classification="unclassified",
            ),
            SearchResult(
                entity_id="b",
                path="person/b.md",
                snippet="",
                score=0.8,
                classification="SECRET",
            ),
            SearchResult(
                entity_id="c",
                path="person/c.md",
                snippet="",
                score=0.5,
                classification="CUI",
            ),
        ]
        filtered = checker.filter_results(results, Classification.CUI)
        entity_ids = [r.entity_id for r in filtered]
        assert "a" in entity_ids
        assert "c" in entity_ids
        assert "b" not in entity_ids  # SECRET filtered from CUI clearance

    def test_filter_index_entries(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        checker = ClassificationChecker(config)
        entries = [
            IndexEntry(
                entity_id="a",
                path="person/a.md",
                entity_type="person",
                classification="unclassified",
            ),
            IndexEntry(
                entity_id="b",
                path="person/b.md",
                entity_type="person",
                classification="TOP_SECRET",
            ),
        ]
        filtered = checker.filter_results(entries, Classification.SECRET)
        entity_ids = [e.entity_id for e in filtered]
        assert "a" in entity_ids
        assert "b" not in entity_ids

    def test_filter_personal_allows_all(self) -> None:
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        results = [
            SearchResult(
                entity_id="a",
                path="person/a.md",
                snippet="",
                score=1.0,
                classification="TOP_SECRET",
            ),
        ]
        filtered = checker.filter_results(results, Classification.UNCLASSIFIED)
        assert len(filtered) == 1
