"""Agentic consolidation — the DEFAULT "sleep" engine (a bounded ReAct loop).

Instead of one deterministic structured completion, the default consolidation
engine runs a bounded agent over the memory tools: it reads the recent episodes,
SEARCHES existing cards before writing, extracts durable facts / insights /
procedures, MERGES duplicates, LINKS related memories, and stops when done. Each
tool is individually atomic + audited (see ``arcmemory.tools``), so partial
progress is always safe; if the loop breaches, times out, or arcrun is absent, the
engine returns a ``degraded`` signal and the caller finishes the window with the
deterministic pipeline distiller (no data loss).

This module holds NO arcrun import — it drives the loop through the injectable
:data:`~arcmemory.react_adapter.ReactLoop` seam (default: the single adapter).
"""

from __future__ import annotations

from dataclasses import dataclass

from arcprompt import load_stock

from arcmemory.config import MemoryConfig
from arcmemory.react_adapter import ReactLoop, run_react_loop
from arcmemory.tools import MemoryTool
from arcmemory.types import Event


@dataclass
class AgenticResult:
    """Outcome of one agentic consolidation pass.

    ``degraded`` is the fallback signal: on True the caller runs the deterministic
    pipeline distiller for the same window (``reason`` explains why — a breach
    label, ``timeout``, or ``arcrun-absent``).
    """

    degraded: bool = False
    reason: str | None = None
    turns: int = 0
    tool_calls_made: int = 0


def _render_task(episodes: list[Event]) -> str:
    """Render the window as chronological, id-anchored lines for the agent to read."""
    lines = "\n".join(f"- [{e.event_id}] {e.ts[11:16]} ({e.kind}) {e.text}" for e in episodes)
    return f"Consolidate this window of {len(episodes)} raw episode(s):\n{lines}"


async def run_agentic_consolidation(
    *,
    episodes: list[Event],
    model: object,
    tools: list[MemoryTool],
    config: MemoryConfig,
    actor_did: str,
    react_loop: ReactLoop = run_react_loop,
) -> AgenticResult:
    """Run one bounded agentic consolidation; never raise, degrade on breach/timeout.

    Caps are TIGHT (from ``config``): a bounded number of turns/tokens and a
    wall-clock timeout, all enforced by the adapter. Returns ``degraded=True`` when
    the loop could not complete cleanly so the caller can fall back.
    """
    if not episodes:
        return AgenticResult()
    outcome = await react_loop(
        model=model,
        tools=tools,
        system_prompt=load_stock("arcmemory", "consolidate_agent"),
        task=_render_task(episodes),
        max_turns=config.consolidate_agent_max_turns,
        max_tokens=config.consolidate_agent_max_tokens,
        timeout_seconds=config.consolidate_agent_timeout_seconds,
        actor_did=actor_did,
    )
    return AgenticResult(
        degraded=outcome.degraded,
        reason=outcome.reason,
        turns=outcome.turns,
        tool_calls_made=outcome.tool_calls_made,
    )


__all__ = ["AgenticResult", "run_agentic_consolidation"]
