"""Hard privacy filter — the raise-only safety net (SPEC-083 COMP-003).

This is a security control, not a heuristic nicety: it can only ever RAISE an
item's effective score toward the private band, never lower it. When in doubt it
over-classifies (favor false-positives over misses), because a missed secret that
promotes to the shared store is the failure that matters (REQ-434, REQ-444).

The deterministic rules (secret/credential, email, phone) are real regexes here.
The "does this reference another person or another session" judgement needs a
model, so it is INJECTED as a callable — this module only accepts and calls it,
and never imports or invokes a model itself (keeps the filter pure and testable).
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: Private floor: a hit pushes the item to the top of the never band (>= 8).
_PRIVATE_FLOOR = 8
#: Clean floor: nothing suspicious — the item imposes no privacy constraint.
_CLEAN_FLOOR = 1

#: Secret/credential keywords. Conservative on purpose: the presence of the WORD
#: is enough to treat the whole item as private, even without the secret value.
_SECRET_RE = re.compile(
    r"\b(passwd|password|secret|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|token|credential|bearer)\b",
    re.IGNORECASE,
)

#: Email address — PII. Standard ``local@domain.tld`` shape.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Phone number — PII. Matches a 10-digit run split by ``-``, ``.`` or spaces
#: (e.g. ``555-123-4567``). Deliberately loose: over-matching an ordinary number
#: is a cheap false-positive; missing a real phone number is not.
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")


def privacy_floor(
    item: str,
    *,
    other_person_check: Callable[[str], bool] | None = None,
) -> int:
    """Return the minimum effective score ``item`` may carry.

    Any deterministic PII/secret hit, or an injected ``other_person_check``
    returning True, floors the item into the never band (>= 8). Any exception
    raised by ``other_person_check`` also floors to 8 — a classifier that blows
    up must not open the gate (fail-closed). A clean item gets a low floor (1).
    """
    if _SECRET_RE.search(item) or _EMAIL_RE.search(item) or _PHONE_RE.search(item):
        return _PRIVATE_FLOOR
    if other_person_check is not None:
        try:
            if other_person_check(item):
                return _PRIVATE_FLOOR
        except Exception:
            # Fail-closed: an exploding classifier is treated as "private", never
            # as "clean". Deliberate broad catch at a security boundary.
            return _PRIVATE_FLOOR
    return _CLEAN_FLOOR


def effective_score(
    personal_score: int | None,
    item: str,
    *,
    other_person_check: Callable[[str], bool] | None = None,
) -> int:
    """The score the router acts on: ``max(personal_score or 8, privacy_floor)``.

    Raise-only — it never lowers a score. An unscored (``None``) or falsy score
    is treated as 8 (fail-closed), so a clean-but-unscored item still lands in
    the never band; a genuine low score on a clean item is preserved as-is.
    """
    return max(
        personal_score or _PRIVATE_FLOOR,
        privacy_floor(item, other_person_check=other_person_check),
    )


__all__ = ["effective_score", "privacy_floor"]
