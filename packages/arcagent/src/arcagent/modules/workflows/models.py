"""Builder-surface types for the workflows module (SPEC-061 COMP-012).

Three things live here and nothing else:

1. **The field allowlists.** Every mutating builder tool projects its caller's
   arguments through an explicit allowlist before anything reaches the control
   plane (ASI02). A model that invents ``model``, ``temperature``, ``status``,
   or ``signature`` on a node has those fields silently dropped rather than
   forwarded — a workflow declares *topology*, never LLM wire-control, and
   never its own trust status.
2. **Inline free-text normalization.** NFKC folding plus zero-width stripping
   *before* the injection scan, so a homoglyph or a split token cannot walk an
   instruction past the regex (LLM01, the BLOCKING finding of the 2026-02-16
   scheduler hardening review).
3. **The typed issue shape.** ``{node_id, field, error, observed, admissible}``
   — identical to ``arcteam.workflow.ValidationIssue`` so an agent repairing a
   rejection sees ONE shape whether the refusal came from this tool boundary or
   from the graph validator underneath it (REQ-222). Admissible alternatives are
   what actually drive repair, so a refusal that can name them must.

The workflow document model itself is arcteam's (COMP-001) and is deliberately
not mirrored here — a second copy of the schema is a second source of truth.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Zero-width characters used in Unicode homoglyph / split-token attacks.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")

# Hard prompt-injection patterns. Matched against NFKC-normalized text so a
# full-width or zero-width-split payload folds to ASCII first.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+previous\b", re.IGNORECASE),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
    re.compile(r"\binstead\b.*\bdo\b", re.IGNORECASE),
    re.compile(r"\bsystem:", re.IGNORECASE),
    re.compile(r"\bassistant:", re.IGNORECASE),
    re.compile(r"\bexfiltrate\b", re.IGNORECASE),
    re.compile(r"\bforget\b.*\binstructions?\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\boverride\b", re.IGNORECASE),
    re.compile(r"<\|[a-z_]+\|>", re.IGNORECASE),  # role delimiters like <|system|>
    re.compile(r"\bbase64\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+follow\b", re.IGNORECASE),
)

# Fields a builder tool may set on a node. Everything else is dropped.
# Deliberately absent: model/temperature (LLM wire-control belongs to the agent,
# never the workflow — DESIGN §4), and status/content_hash/signature (trust
# status is a property of the signed bundle, never of an authored document).
NODE_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "kind",
        "agent",
        "needs",
        "join",
        "when",
        "skill",
        "strategy",
        "prompt",
        "tool",
        "args",
        "script",
        "gate",
        "mode",
        "routes",
        "output_schema",
        "artifacts",
        "loop_back_to",
        "max_iterations",
        "timeout_s",
        "max_attempts",
    }
)

# Fields a builder tool may set on the workflow header.
WORKFLOW_FIELDS: frozenset[str] = frozenset({"description", "owner", "channel", "budget"})

# Fields a builder tool may set on a trigger.
TRIGGER_FIELDS: frozenset[str] = frozenset(
    {"type", "expression", "at", "every_seconds", "active_hours"}
)

# Node fields that carry inline free text and must be normalized + scanned.
# ``prompt`` is a FILE REFERENCE, not prose, so it is not in this set — node
# instructions live in signed files precisely so they are not an unsigned
# instruction surface.
INLINE_TEXT_FIELDS: frozenset[str] = frozenset({"description"})


def issue(
    *,
    field: str,
    error: str,
    node_id: str | None = None,
    observed: Any = None,
    admissible: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Build one typed validation issue (REQ-222).

    Same five keys as ``arcteam.workflow.ValidationIssue`` so an agent repairing
    a rejection never has to branch on where the refusal came from.
    """
    return {
        "node_id": node_id,
        "field": field,
        "error": error,
        "observed": observed,
        "admissible": list(admissible),
    }


def normalize_inline_text(text: str, *, max_length: int) -> str:
    """NFKC-normalize, strip zero-width chars, then reject injection or overrun.

    Raises ``ValueError`` on refusal. The normalized form is what is RETURNED
    and stored — scanning a normalized string but persisting the raw one would
    leave the payload intact for whatever reads it next.
    """
    normalized = _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", text))
    if len(normalized) > max_length:
        raise ValueError(f"text exceeds maximum length ({len(normalized)} > {max_length})")
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(normalized):
            raise ValueError("text rejected: possible injection pattern detected")
    return normalized


def project(source: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Return only the allowlisted keys of ``source`` (ASI02 least privilege)."""
    return {key: value for key, value in source.items() if key in allowed}


__all__ = [
    "INLINE_TEXT_FIELDS",
    "NODE_FIELDS",
    "TRIGGER_FIELDS",
    "WORKFLOW_FIELDS",
    "issue",
    "normalize_inline_text",
    "project",
]
