"""Task model re-exports + free-text validation for the ``tasks`` module.

``Task``, ``TaskStatus``, ``Priority`` are the arcstore-owned durable model
(SPEC-056 Phase A) — re-exported here so tool code only ever imports from
this module, mirroring the scheduler template's ``models.py``.
``validate_task_text`` is a standalone copy of the scheduler's NFKC +
injection-regex prompt validator (``arcagent/modules/scheduler/models.py``)
rather than a cross-module import — each module stays independent per
structure.md's "each module independent" rule.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from arcstore.tasks import Priority, Task, TaskStatus

ClaimReason = Literal["assigned", "continue_current", "no_tasks_available"]

DEFAULT_MAX_TEXT_LENGTH = 2000

# Zero-width characters used in Unicode homoglyph attacks.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")

# Patterns that indicate prompt injection attempts.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bignore\s+previous\b", re.IGNORECASE),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
    re.compile(r"\binstead\b.*\bdo\b", re.IGNORECASE),
    re.compile(r"\bsystem:", re.IGNORECASE),
    re.compile(r"\bassistant:", re.IGNORECASE),
    re.compile(r"\bexfiltrate\b", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bforget\b.*\binstructions?\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\boverride\b", re.IGNORECASE),
    re.compile(r"<\|[a-z_]+\|>", re.IGNORECASE),  # role delimiters like <|system|>
    re.compile(r"\bbase64\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+follow\b", re.IGNORECASE),
]


def _normalize_text(text: str) -> str:
    """Normalize Unicode and strip zero-width characters.

    NFKC normalization collapses homoglyphs (e.g. full-width Latin) to
    their ASCII equivalents, preventing regex bypass.
    """
    normalized = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH_RE.sub("", normalized)


def validate_task_text(text: str, *, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> bool:
    """Validate task free-text (title/description/resolution) for length and
    injection patterns (LLM01/ASI06). Raises ValueError on failure, returns
    True on success.
    """
    if len(text) > max_length:
        msg = f"Text exceeds maximum length ({len(text)} > {max_length})"
        raise ValueError(msg)
    normalized = _normalize_text(text)
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(normalized):
            msg = "Text rejected: possible injection pattern detected"
            raise ValueError(msg)
    return True


__all__ = ["ClaimReason", "Priority", "Task", "TaskStatus", "validate_task_text"]
