"""Mid-loop recall buffer — the per-run staging point for a decision-point recall.

SPEC-072 COMP-003. A decision-point recall is produced mid-loop (the memory module's
``agent:moment`` subscriber calls the Brain), but the system prompt was already assembled
at run start, so there is nowhere to inject it — except arcrun's existing append-only
``transform_context`` hook, which runs before every model call. This module is the shared
hand-off between the two:

* the memory module (a module → core dependency) STAGES a rendered block here, keyed by
  the agent's DID;
* :meth:`ContextManager.transform_context` (core) DRAINS it and appends it to the message
  tail before the next model call — append-only, prefix-stable.

A plain module-global dict keyed by DID (mirroring the memory runtime's own registry) is
used deliberately: the moment subscriber runs in a sibling task the bridge schedules, so a
``contextvars``-isolated buffer written there would be invisible to the loop task. A shared
dict is visible across tasks, and DID-keying keeps one agent's staged block out of another's
(shared-nothing, LLM08). The buffer never persists — it is drained and cleared each turn.

This module holds no memory logic and imports nothing from arcmemory: the block is an
opaque, already-gated string the Brain rendered. arcrun stays unaware it exists.
"""

from __future__ import annotations

# Per-agent staged blocks, keyed by DID. A module-global (not a contextvar) so a block
# staged in the subscriber's sibling task is visible when the loop task drains it.
_buffers: dict[str, list[str]] = {}


def stage(agent_did: str, block: str) -> None:
    """Stage one rendered recall block for ``agent_did`` (empty blocks ignored)."""
    if not agent_did or not block:
        return
    _buffers.setdefault(agent_did, []).append(block)


def drain(agent_did: str) -> list[str]:
    """Return and CLEAR the staged blocks for ``agent_did`` (``[]`` when none)."""
    buffer = _buffers.get(agent_did)
    if not buffer:
        return []
    drained = list(buffer)
    buffer.clear()
    return drained


__all__ = ["drain", "stage"]
