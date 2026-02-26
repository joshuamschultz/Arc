"""Tests for compact fact triplets — format, parse, contradiction detection."""

from __future__ import annotations

import pytest

from arcagent.modules.bio_memory.facts import (
    Fact,
    find_contradiction,
    format_fact,
    parse_fact,
    parse_facts,
)


class TestFormatFact:
    """format_fact() produces compact triplet lines."""

    def test_basic_format(self) -> None:
        line = format_fact("works_at", "Anthropic", 0.9, "2024-03-15")
        assert line == "- works_at: Anthropic .9 2024-03-15"

    def test_high_confidence(self) -> None:
        line = format_fact("is_alive", "true", 1.0, "2026-01-01")
        assert line == "- is_alive: true 1 2026-01-01"

    def test_mid_confidence(self) -> None:
        line = format_fact("prefers", "Python", 0.85, "2025-06-10")
        assert line == "- prefers: Python .85 2025-06-10"

    def test_low_confidence(self) -> None:
        line = format_fact("hobby", "sailing", 0.5, "2024-01-01")
        assert line == "- hobby: sailing .5 2024-01-01"

    def test_with_contradiction(self) -> None:
        line = format_fact(
            "works_at", "Cisco", 0.9, "2026-02-25",
            was_value="IBM", was_confidence=0.8,
        )
        assert line == "- works_at: Cisco .9 2026-02-25 | was: IBM .8"

    def test_default_date_uses_today(self) -> None:
        line = format_fact("role", "Engineer", 0.7)
        # Should contain a date in YYYY-MM-DD format
        assert len(line.split()) >= 4
        # Last token should be a date
        parts = line.rsplit(" ", 1)
        assert len(parts[1]) == 10  # YYYY-MM-DD

    def test_sanitizes_long_values(self) -> None:
        long_value = "x" * 1000
        line = format_fact("note", long_value, 0.5, "2024-01-01")
        # sanitize_text truncates at 500
        assert len(line) < 1000


class TestParseFact:
    """parse_fact() extracts structured data from triplet lines."""

    def test_basic_parse(self) -> None:
        fact = parse_fact("- works_at: Anthropic .9 2024-03-15")
        assert fact is not None
        assert fact.predicate == "works_at"
        assert fact.value == "Anthropic"
        assert fact.confidence == 0.9
        assert fact.date == "2024-03-15"
        assert fact.was_value is None

    def test_parse_with_contradiction(self) -> None:
        fact = parse_fact("- works_at: Cisco .9 2026-02-25 | was: IBM .8")
        assert fact is not None
        assert fact.value == "Cisco"
        assert fact.was_value == "IBM"
        assert fact.was_confidence == 0.8

    def test_parse_confidence_1(self) -> None:
        fact = parse_fact("- is_human: true 1 2024-01-01")
        assert fact is not None
        assert fact.confidence == 1.0

    def test_parse_multi_word_value(self) -> None:
        fact = parse_fact("- role: Staff Engineer .85 2026-01-10")
        assert fact is not None
        assert fact.value == "Staff Engineer"

    def test_returns_none_for_non_fact(self) -> None:
        assert parse_fact("Just a regular line") is None
        assert parse_fact("## Key Facts") is None
        assert parse_fact("") is None

    def test_returns_none_for_plain_bullet(self) -> None:
        assert parse_fact("- some bullet without triplet format") is None

    def test_roundtrip(self) -> None:
        """format -> parse -> same data."""
        original = format_fact("works_at", "Anthropic", 0.9, "2024-03-15")
        parsed = parse_fact(original)
        assert parsed is not None
        assert parsed.predicate == "works_at"
        assert parsed.value == "Anthropic"
        assert parsed.confidence == 0.9
        assert parsed.date == "2024-03-15"

    def test_roundtrip_with_contradiction(self) -> None:
        original = format_fact(
            "role", "CTO", 0.95, "2026-02-25",
            was_value="VP", was_confidence=0.85,
        )
        parsed = parse_fact(original)
        assert parsed is not None
        assert parsed.value == "CTO"
        assert parsed.was_value == "VP"
        assert parsed.was_confidence == 0.85


class TestParseFacts:
    """parse_facts() extracts all triplets from markdown body."""

    def test_extracts_from_mixed_content(self) -> None:
        text = (
            "## Key Facts\n"
            "- works_at: Anthropic .9 2024-03-15\n"
            "- role: Engineer .85 2024-03-15\n"
            "\n"
            "## Summary\n"
            "Some prose here.\n"
            "- not a fact line\n"
        )
        facts = parse_facts(text)
        assert len(facts) == 2
        assert facts[0].predicate == "works_at"
        assert facts[1].predicate == "role"

    def test_empty_text(self) -> None:
        assert parse_facts("") == []

    def test_no_facts(self) -> None:
        assert parse_facts("## Key Facts\n\nNothing here.\n") == []


class TestFindContradiction:
    """find_contradiction() detects changed facts."""

    def test_finds_contradiction(self) -> None:
        existing = [
            Fact("works_at", "IBM", 0.8, "2023-01-10"),
            Fact("role", "Manager", 0.7, "2023-06-01"),
        ]
        result = find_contradiction(existing, "works_at", "Cisco")
        assert result is not None
        assert result.value == "IBM"

    def test_no_contradiction_same_value(self) -> None:
        existing = [Fact("works_at", "IBM", 0.8, "2023-01-10")]
        result = find_contradiction(existing, "works_at", "IBM")
        assert result is None

    def test_no_contradiction_new_predicate(self) -> None:
        existing = [Fact("works_at", "IBM", 0.8, "2023-01-10")]
        result = find_contradiction(existing, "role", "Engineer")
        assert result is None

    def test_empty_existing(self) -> None:
        result = find_contradiction([], "works_at", "Anthropic")
        assert result is None
