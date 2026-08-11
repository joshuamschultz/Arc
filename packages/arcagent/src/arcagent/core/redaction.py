"""ArcAgent-owned redaction service built from neutral ArcTrust primitives."""

from __future__ import annotations

from collections.abc import Callable

import arctrust


def default_redactor() -> Callable[[str], str]:
    """Return the safe default redactor for ArcAgent-built approval payloads."""
    detector = arctrust.RegexPiiDetector()

    def redact(text: str) -> str:
        matches = detector.detect(text)
        return arctrust.redact_text(text, matches) if matches else text

    return redact


__all__ = ["default_redactor"]
