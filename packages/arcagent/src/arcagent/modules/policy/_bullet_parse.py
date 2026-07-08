"""Shared policy-bullet parsing — the single source of truth for the
structured ``policy.md`` bullet grammar.

Both the :class:`~arcagent.modules.policy.policy_engine.PolicyEngine` curator
and the read-only ``arc agent policy`` CLI inspector parse the same bullet
format. This module owns the regex and the raw parse so the two can never
drift. It imports only :mod:`re`, so the CLI can use it without pulling in
the engine's heavy dependencies (arcllm, workspace IO).
"""

from __future__ import annotations

import re

# Grammar for a structured policy bullet:
#   - [P01] text {score:5, uses:0, reviewed:..., created:..., source:...}
_BULLET_RE = re.compile(
    r"^-\s+\[(?P<id>P\d+)\]\s+(?P<text>.+?)\s+"
    r"\{score:(?P<score>\d+),\s*uses:(?P<uses>\d+),\s*"
    r"reviewed:(?P<reviewed>[^,]+),\s*created:(?P<created>[^,]+),\s*"
    r"source:(?P<source>[^}]*)\}",
)


def parse_bullets(content: str) -> list[dict[str, str]]:
    """Parse structured bullets from ``policy.md`` content into raw dicts.

    Returns one dict of string fields per matched bullet line. Callers that
    need typed values (e.g. the engine's ``PolicyBullet``) cast on top of
    these raw strings.
    """
    results: list[dict[str, str]] = []
    for line in content.split("\n"):
        match = _BULLET_RE.match(line.strip())
        if match:
            results.append(
                {
                    "id": match.group("id"),
                    "text": match.group("text").strip(),
                    "score": match.group("score"),
                    "uses": match.group("uses"),
                    "reviewed": match.group("reviewed").strip(),
                    "created": match.group("created").strip(),
                    "source": match.group("source").strip(),
                }
            )
    return results


__all__ = ["parse_bullets"]
