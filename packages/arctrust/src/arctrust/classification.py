"""Canonical classification ladder + dominance comparator (SPEC-038 REQ-020).

arctrust owns the classification TYPE system (per CLAUDE.md). This is the
single ordered ladder for the whole codebase; siblings (arcteam, arcagent)
import ``Classification`` and ``dominates`` from here — they never redefine it.

The ladder mirrors the isolation-ladder pattern in ``policy.py``: an ordered
lattice with a total-order ``dominates`` comparator realizing the Bell-LaPadula
"no read up" predicate (a subject may read an object only when its clearance
dominates the object's classification). NIST 800-53 AC-4.

Compartments/caveats (NOFORN, SCI) are deliberately out of scope for now.
"""

from __future__ import annotations

import logging
from enum import IntEnum

logger = logging.getLogger(__name__)


class Classification(IntEnum):
    """US Government classification hierarchy (total order, low to high)."""

    UNCLASSIFIED = 0
    CUI = 1
    CONFIDENTIAL = 2
    SECRET = 3
    TOP_SECRET = 4


def dominates(clearance: Classification, resource: Classification) -> bool:
    """True iff ``clearance`` is cleared for ``resource`` — the lattice ``⊒``.

    A total-order integer compare: a subject dominates a resource when its
    clearance is at least as high. This is the "no read up" / "no write down"
    predicate shared by the ClassificationLayer, the messenger, and egress.
    """
    return clearance >= resource


def parse_classification(value: str, *, strict: bool) -> Classification:
    """Parse a classification label to the ladder.

    ``strict=True`` (federal): an unknown or empty label raises ``ValueError``
    — fail closed, never default-permissive (REQ-026). ``strict=False``
    (personal/enterprise): an unknown label warns and defaults to
    ``UNCLASSIFIED`` (the historical arcteam behavior).
    """
    normalized = value.upper().strip()
    if not normalized:
        if strict:
            raise ValueError("empty classification label is not permitted (fail closed)")
        return Classification.UNCLASSIFIED
    try:
        return Classification[normalized]
    except KeyError as err:
        if strict:
            raise ValueError(f"unknown classification label {value!r} (fail closed)") from err
        logger.warning("Unknown classification value %r, defaulting to UNCLASSIFIED", value)
        return Classification.UNCLASSIFIED


__all__ = ["Classification", "dominates", "parse_classification"]
