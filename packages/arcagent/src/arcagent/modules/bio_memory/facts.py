"""Compact fact triplets for bio-memory entity files.

Format: ``- predicate: value .confidence YYYY-MM-DD``
Contradiction trail: ``- predicate: value .confidence YYYY-MM-DD | was: old_value .old_confidence``

Token-efficient structured facts that support:
- Staleness detection via per-fact dates
- Contradiction tracking via ``| was:`` suffix
- Confidence scoring for fact reliability
- Regex-parseable for programmatic access
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import NamedTuple

from arcagent.utils.sanitizer import sanitize_text

# Maximum text length per fact value (ASI-06 defense)
_MAX_FACT_TEXT = 500

# Parse: "- predicate: value .confidence YYYY-MM-DD"
# Optional: " | was: old_value .old_confidence"
FACT_RE = re.compile(
    r"^-\s+(.+?):\s+(.+?)\s+"
    r"(\.\d+|1)\s+"
    r"(\d{4}-\d{2}-\d{2})"
    r"(?:\s+\|\s+was:\s+(.+?)\s+(\.\d+|1))?$"
)


class Fact(NamedTuple):
    """Parsed fact triplet."""

    predicate: str
    value: str
    confidence: float
    date: str
    was_value: str | None = None
    was_confidence: float | None = None


def format_fact(
    predicate: str,
    value: str,
    confidence: float = 0.5,
    date: str | None = None,
    was_value: str | None = None,
    was_confidence: float | None = None,
) -> str:
    """Format a fact as a compact triplet line.

    >>> format_fact("works_at", "Anthropic", 0.9, "2024-03-15")
    '- works_at: Anthropic .9 2024-03-15'
    """
    if date is None:
        date = datetime.now(UTC).strftime("%Y-%m-%d")

    predicate = sanitize_text(predicate, max_length=_MAX_FACT_TEXT)
    value = sanitize_text(value, max_length=_MAX_FACT_TEXT)
    conf_str = _format_confidence(confidence)
    line = f"- {predicate}: {value} {conf_str} {date}"

    if was_value is not None:
        was_value = sanitize_text(was_value, max_length=_MAX_FACT_TEXT)
        was_conf_str = _format_confidence(was_confidence if was_confidence is not None else 0.5)
        line += f" | was: {was_value} {was_conf_str}"

    return line


def parse_fact(line: str) -> Fact | None:
    """Parse a single fact line. Returns None if not a valid triplet."""
    match = FACT_RE.match(line.strip())
    if not match:
        return None

    was_val = match.group(5)
    was_conf = float(match.group(6)) if match.group(6) else None

    return Fact(
        predicate=match.group(1),
        value=match.group(2),
        confidence=float(match.group(3)),
        date=match.group(4),
        was_value=was_val,
        was_confidence=was_conf,
    )


def parse_facts(text: str) -> list[Fact]:
    """Parse all fact triplet lines from a markdown body."""
    facts: list[Fact] = []
    for line in text.split("\n"):
        fact = parse_fact(line)
        if fact is not None:
            facts.append(fact)
    return facts


def find_contradiction(
    existing: list[Fact],
    predicate: str,
    new_value: str,
) -> Fact | None:
    """Find an existing fact with the same predicate but different value."""
    for fact in existing:
        if fact.predicate == predicate and fact.value != new_value:
            return fact
    return None


def _format_confidence(confidence: float) -> str:
    """Format confidence compactly: 0.9 -> '.9', 0.85 -> '.85', 1.0 -> '1'."""
    if confidence >= 1.0:
        return "1"
    # "0.9" -> ".9", "0.85" -> ".85"
    return str(round(confidence, 2)).lstrip("0")
