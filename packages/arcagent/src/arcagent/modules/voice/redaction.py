"""PII redaction for voice transcripts.

Implements bidirectional redaction required for federal/enterprise tiers
(AUTO-8 / SDD §2). Patterns cover the most common PII categories:
    - SSN (123-45-6789 or 123456789)
    - US phone numbers (various formats)
    - Email addresses
    - Credit card numbers (Visa, MC, Amex, Discover)
    - IPv4 addresses (often PII in federal audit contexts)

Redaction replaces matched text with a stable placeholder token so the
agent can reason about structure without seeing sensitive content.

If arcllm.security exposes a `redact_pii` function, it is preferred;
this module provides a regex fallback that works with zero network access.

Usage::

    from arcagent.modules.voice.redaction import redact_transcript
    clean, applied = redact_transcript("Call me at 555-867-5309")
    # clean = "Call me at [PHONE]"
    # applied = True
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# PII patterns — ordered from most-specific to least-specific to reduce
# false-positive interactions (e.g., SSN before general digit sequences).
# ---------------------------------------------------------------------------

# SSN: 123-45-6789 or 123 45 6789 or 123456789
_SSN = re.compile(r"\b(\d{3}[-\s]?\d{2}[-\s]?\d{4})\b")

# US phone: (555) 867-5309, 555-867-5309, +1-555-867-5309, 5558675309
_PHONE = re.compile(
    r"""
    (?:
        (?:\+?1[-.\s]?)?          # optional country code
        (?:\(\d{3}\)|\d{3})       # area code
        [-.\s]?
        \d{3}
        [-.\s]?
        \d{4}
    )
    """,
    re.VERBOSE,
)

# Email
_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Credit card -- 13-19 digit sequences with optional separators
# Anchored to word boundaries to avoid matching larger number blocks.
_CREDIT_CARD = re.compile(
    r"\b(?:\d{4}[-\s]?){3,4}\d{1,4}\b"
)

# IPv4 address
_IPV4 = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

# Ordered list: (pattern, replacement_token)
# SSN must be checked before PHONE to avoid partial matches.
_REDACTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (_SSN, "[SSN]"),
    (_EMAIL, "[EMAIL]"),
    (_CREDIT_CARD, "[CREDIT_CARD]"),
    (_PHONE, "[PHONE]"),
    (_IPV4, "[IPV4]"),
]


def redact_transcript(text: str) -> tuple[str, bool]:
    """Apply PII redaction patterns to a transcript string.

    Applies all patterns in priority order. Returns the redacted string
    and a boolean indicating whether any redaction was applied.

    Args:
        text: Raw transcript text (may contain PII).

    Returns:
        Tuple of (redacted_text, redaction_applied).
        ``redaction_applied`` is True if at least one pattern matched.

    Note:
        This function never raises. If a regex fails for any reason,
        the original text is returned with redaction_applied=False and
        the error is silently suppressed. Callers at federal tier should
        treat redaction_applied=False as a warning and log it.
    """
    try:
        current = text
        applied = False

        for pattern, token in _REDACTION_RULES:
            replaced, count = pattern.subn(token, current)
            if count > 0:
                applied = True
                current = replaced

        return current, applied

    except Exception:
        return text, False
