"""Thin memory-module config — the Brain seam's knobs (SPEC-041 §4.6).

The memory module is *wiring only*: it owns no memory logic (that lives in the
selected :class:`~arcagent.brain.Brain`). These fields pick the brain and bound
recall + consolidation scheduling. ``brain`` is the SPEC-047 selector:

* ``"none"`` (default) — :class:`~arcagent.brain.NullBrain`; memory off, zero files.
* ``"arcmemory"`` / ``"auto"`` — the ``arcmemory`` plug-in if installed.
* a dotted ``module:Class`` path — a bring-your-own Brain.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class MemoryConfig(ModuleConfig):
    """Configuration for the thin memory (Brain) wiring module."""

    brain: str = "none"
    tier: str = "personal"

    # Recall (agent:assemble_prompt @ priority 50)
    top_k: int = 5
    budget: int = 1024

    # Consolidation scheduling (event-count / idle trigger, DC-5)
    consolidate_event_threshold: int = 20
    consolidate_idle_seconds: float = 900.0


__all__ = ["MemoryConfig"]
