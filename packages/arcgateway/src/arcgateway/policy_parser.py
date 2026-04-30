"""ACE policy bullet parser — pure function, text in / dataclass out.

ACE bullets in ``team/<agent>/workspace/policy.md`` follow the format::

    - [P##] <text> {score:N, uses:N, reviewed:YYYY-MM-DD, created:YYYY-MM-DD, source:<sid>}

This module parses those lines into :class:`PolicyBullet` instances. Any line
that does not match the bullet shape is silently skipped, so parser is safe to
run over arbitrary markdown content.

Rationale (Pillar 1 — Simplicity, Pillar 2 — Modularity):
    Same parser is consumed by the per-agent Policy tab AND the fleet Policy
    Engine page in arcui. Keeping it pure (no I/O, no logging, no audit) means
    both call sites get identical behavior with zero coupling.

Retirement convention:
    A bullet whose ``score`` is ≤ 2 is considered retired (PRD §F8.7 filter
    pill "Retired"). The ``retired`` field is derived, not stored, so callers
    don't need to recompute it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# - [P##] <text> {meta}
# Greedy on text, lazy on meta block.
_BULLET_RE = re.compile(
    r"^\s*-\s*\[(?P<id>P\d+)\]\s*"
    r"(?P<text>.+?)\s*"
    r"\{(?P<meta>[^}]+)\}\s*$"
)
# key: value pairs separated by commas inside the meta block.
_META_RE = re.compile(r"(\w+)\s*:\s*([^,}]*)")


@dataclass(frozen=True)
class PolicyBullet:
    """A single parsed ACE policy bullet.

    Attributes:
        id: Bullet identifier (e.g. ``P01``). Includes the ``P`` prefix.
        text: Bullet body, with surrounding whitespace stripped.
        score: Integer in range typically 0-10. May be negative for malformed
            input; callers should treat ``retired`` as the active/inactive flag
            rather than comparing score directly.
        uses: Recorded use count. Defaults to 0 if absent.
        reviewed: Date of last reflector review, ``None`` if absent or invalid.
        created: Date of bullet creation, ``None`` if absent or invalid.
        source: Source session id (``sid``). Empty string if absent.
        retired: Derived from ``score <= 2``. Retired bullets are still parsed
            and surfaced — UI filters them on the "Retired" pill.
    """

    id: str
    text: str
    score: int
    uses: int
    reviewed: date | None
    created: date | None
    source: str
    retired: bool


def parse_bullets(text: str) -> list[PolicyBullet]:
    """Parse all ACE policy bullets from a chunk of markdown text.

    Lines that don't match the bullet shape are skipped silently. Field
    defaults: ``score=5``, ``uses=0``, ``source=""``, dates ``None``.

    Args:
        text: Raw markdown text (e.g. contents of ``policy.md``).

    Returns:
        List of :class:`PolicyBullet` in source order.
    """
    out: list[PolicyBullet] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if m is None:
            continue
        meta = dict(_META_RE.findall(m.group("meta")))
        score = _parse_int(meta.get("score"), default=5)
        out.append(
            PolicyBullet(
                id=m.group("id"),
                text=m.group("text").strip(),
                score=score,
                uses=_parse_int(meta.get("uses"), default=0),
                reviewed=_parse_date(meta.get("reviewed")),
                created=_parse_date(meta.get("created")),
                source=(meta.get("source") or "").strip(),
                retired=score <= 2,
            )
        )
    return out


def _parse_int(s: str | None, *, default: int) -> int:
    if s is None:
        return default
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def _parse_date(s: str | None) -> date | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
