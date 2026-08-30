"""Trust is pinned in fleet code, never carried on a descriptor (H-040 §2.1).

The self-blessing trap: a folder-discovered :class:`~arcteam.harness.agent_type.AgentType`
must never be able to declare its own ``trusted`` or ``sandbox`` — a foreign
extension folder could ship ``trusted=True`` and bless itself. So the rule lives
HERE, in the fleet's own code, and nowhere else:

- **Trusted iff ``harness == "arcagent"``** — the single in-tree implementation,
  known to the fleet by identity, not by a flag it carries. Every folder-scanned
  type is untrusted, unconditionally (ASI04).
- **Sandbox placement is the fleet's decision from that trust**, never the
  descriptor's: native may run in-process; every foreign type runs
  out-of-process — mandatory, all tiers (§9, §13 ruling 2).
"""

from __future__ import annotations

from typing import Literal

#: The single trusted, in-tree harness. Pinned here — never a descriptor field.
NATIVE_HARNESS = "arcagent"

Placement = Literal["in_process", "out_of_process"]


def is_trusted(harness: str) -> bool:
    """Whether the fleet trusts this harness kind. True ONLY for native arcagent."""
    return harness == NATIVE_HARNESS


def sandbox_placement(harness: str) -> Placement:
    """Where the fleet runs this harness — its decision, not the descriptor's.

    Native arcagent may run in-process; every foreign harness runs
    out-of-process, mandatory at every tier (§9). A descriptor cannot request
    in-process, because it never gets a say — the answer is a pure function of
    the pinned trust rule above.
    """
    return "in_process" if is_trusted(harness) else "out_of_process"


__all__ = ["NATIVE_HARNESS", "Placement", "is_trusted", "sandbox_placement"]
