"""PII detection and redaction — regex-based with pluggable override."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from arcllm.exceptions import ArcLLMConfigError


@dataclass(frozen=True, eq=True)
class PiiMatch:
    """A single PII detection result."""

    pii_type: str
    start: int
    end: int
    matched_text: str


@runtime_checkable
class PiiDetector(Protocol):
    """Protocol for PII detection backends."""

    def detect(self, text: str) -> list[PiiMatch]: ...


# ---------------------------------------------------------------------------
# Built-in regex patterns
# ---------------------------------------------------------------------------

_BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "CREDIT_CARD",
        re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
    ),
    (
        "EMAIL",
        re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
    ),
    (
        "PHONE",
        re.compile(
            r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
        ),
    ),
    ("IPV4", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
]


class RegexPiiDetector:
    """PII detector using compiled regex patterns.

    Ships with built-in patterns for SSN, credit card, email, phone, and IPv4.
    Accepts additional custom patterns via constructor.
    """

    def __init__(
        self,
        custom_patterns: list[dict[str, str]] | None = None,
    ) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = list(_BUILTIN_PATTERNS)
        if custom_patterns:
            for entry in custom_patterns:
                name = entry["name"]
                try:
                    compiled = re.compile(entry["pattern"])
                except re.error as e:
                    raise ArcLLMConfigError(
                        f"Invalid regex for custom PII pattern '{name}': {e}"
                    )
                self._patterns.append((name, compiled))

    def detect(self, text: str) -> list[PiiMatch]:
        """Scan text for PII patterns.

        Returns non-overlapping matches sorted by start position.
        When matches overlap, the longer match takes priority.
        """
        if not text:
            return []

        all_matches: list[PiiMatch] = []
        for pii_type, pattern in self._patterns:
            for m in pattern.finditer(text):
                all_matches.append(
                    PiiMatch(
                        pii_type=pii_type,
                        start=m.start(),
                        end=m.end(),
                        matched_text=m.group(),
                    )
                )

        if not all_matches:
            return []

        # Sort by start position, then by length descending (longer wins)
        all_matches.sort(key=lambda m: (m.start, -(m.end - m.start)))

        # Remove overlapping matches (keep first = longest at each position)
        filtered: list[PiiMatch] = []
        last_end = -1
        for match in all_matches:
            if match.start >= last_end:
                filtered.append(match)
                last_end = match.end

        return filtered


def redact_text(text: str, matches: list[PiiMatch]) -> str:
    """Replace PII matches with [PII:TYPE] placeholders.

    Processes matches in reverse order to preserve string indices.
    """
    if not matches:
        return text

    # Process in reverse order so earlier replacements don't shift indices
    result = text
    for match in reversed(matches):
        result = result[: match.start] + f"[PII:{match.pii_type}]" + result[match.end :]
    return result
