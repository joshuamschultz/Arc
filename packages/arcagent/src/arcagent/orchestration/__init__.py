"""Agent orchestration layer — agent's interface to arcrun execution.

This module owns spawn primitives that drive sub-runs of arcrun. It
sits *above* arcrun (arcrun is a pure loop): ``make_spawn_tool`` is the
LLM-facing ``spawn_task`` tool the loop dispatches with live context.

The split:
  - ``arcrun`` — runs one loop. No spawn knowledge.
  - ``arcagent.orchestration`` — primitives that spawn sub-loops:
        ``make_spawn_tool``  — LLM-facing tool factory (``spawn_task``)
        ``spawn``            — async function that starts one child run
        ``spawn_many``       — parallel multi-child entry point
        ``RootTokenBudget``  — shared token pool across parent + children
        ``SpawnResult``      — structured return type
        ``SpawnSpec``        — declarative spec for ``spawn_many``
        ``TokenUsage``       — per-run token counters
"""

from __future__ import annotations

from arcagent.orchestration.prompts import SPAWN_GUIDANCE
from arcagent.orchestration.spawn import (
    RootTokenBudget,
    SpawnResult,
    SpawnSpec,
    TokenUsage,
    make_spawn_tool,
    spawn,
    spawn_many,
)

__all__ = [
    "SPAWN_GUIDANCE",
    "RootTokenBudget",
    "SpawnResult",
    "SpawnSpec",
    "TokenUsage",
    "make_spawn_tool",
    "spawn",
    "spawn_many",
]
