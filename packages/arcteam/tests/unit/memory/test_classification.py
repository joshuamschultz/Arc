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
    """Personal tier: classification check runs at all tiers (ADR-019 four-pillars-universal).

    Previously, personal tier returned True immediately — no enforcement.
    Per ADR-019, 'authorize' is a pillar; tier sets stringency, not whether to enforce.

    Default behavior (unclassified entities + UNCLASSIFIED agent) is still permissive,
    so solo developer workflows are unaffected. But an operator who explicitly classifies
    entities at personal tier gets enforcement.

    Tests that previously asserted "personal tier always returns True regardless of
    classification" are flipped here to assert the new universal enforcement behavior.
    """

    def test_personal_tier_default_permissive_allows_all(self) -> None:
        """Personal tier with default UNCLASSIFIED entity and UNCLASSIFIED agent: allowed.

        ADR-019: default classification is UNCLASSIFIED (most permissive), so by default
        no access filtering happens at personal tier in practice.
        """
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        # UNCLASSIFIED entity, UNCLASSIFIED agent — allowed at any tier
        assert checker.check_access("unclassified", Classification.UNCLASSIFIED) is True

    def test_personal_tier_explicit_classification_enforced(self) -> None:
        """Personal tier: explicit SECRET entity blocks UNCLASSIFIED agent.

        ADR-019: an operator at personal tier who explicitly classifies entities
        still gets enforcement. Previously this returned True (bypass) — now it enforces.

        This test was formerly 'personal_allows_all' which asserted True for SECRET+UNCLASSIFIED.
        Flipped: that test verified the personal-tier bypass that ADR-019 removes.
        """
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        # SECRET entity, UNCLASSIFIED agent — must be denied (clearance < classification)
        assert checker.check_access("SECRET", Classification.UNCLASSIFIED) is False

    def test_personal_tier_sufficient_clearance_allows(self) -> None:
        """Personal tier: agent with SECRET clearance can access SECRET entity."""
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        assert checker.check_access("SECRET", Classification.SECRET) is True

    def test_personal_tier_higher_clearance_allows(self) -> None:
        """Personal tier: agent with TOP_SECRET clearance can access SECRET entity."""
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        assert checker.check_access("SECRET", Classification.TOP_SECRET) is True


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

    def test_filter_personal_enforces_classification(self) -> None:
        """Personal tier: filter_results enforces classification — same logic as other tiers.

        ADR-019: the personal-tier 'return results' bypass is removed.
        Previously this test asserted filtered == 1 (bypass). Now it asserts filtered == 0
        because TOP_SECRET entity is above UNCLASSIFIED agent clearance at any tier.
        """
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
        # UNCLASSIFIED agent cannot see TOP_SECRET entity — at any tier
        filtered = checker.filter_results(results, Classification.UNCLASSIFIED)
        assert len(filtered) == 0

    def test_filter_personal_allows_unclassified_entities(self) -> None:
        """Personal tier: UNCLASSIFIED entities pass through for UNCLASSIFIED agent.

        Default personal-tier workflow (no explicit classification) is unaffected.
        """
        config = TeamMemoryConfig(tier="personal")
        checker = ClassificationChecker(config)
        results = [
            SearchResult(
                entity_id="a",
                path="person/a.md",
                snippet="",
                score=1.0,
                classification="unclassified",
            ),
        ]
        filtered = checker.filter_results(results, Classification.UNCLASSIFIED)
        assert len(filtered) == 1
