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

from arcmemory.config import MemoryConfig
from arcmemory.react_adapter import ReactLoop, run_react_loop
from arcmemory.tools import MemoryTool
from arcmemory.types import Event

CONSOLIDATION_SYSTEM_PROMPT = (
    "You are the memory of an executive assistant, running the nightly "
    "consolidation ('sleep') pass. Your job: turn the raw episodes below into "
    "durable, glass-box memory.\n\n"
    "The memory is made of small markdown cards:\n"
    "- ENTITY cards hold fact triplets (predicate: value) about a person, place, "
    "project, company, or deal, and [[wiki-links]] to related cards.\n"
    "- INSIGHT cards are reusable abstractions: a mechanism-level trigger + a few "
    "abstract cues + the instances they generalize.\n"
    "- PROCEDURE cards are reusable how-tos: a title, when_to_use, and ordered steps.\n\n"
    "Record ONLY the USER's durable domain knowledge — the people, places, projects, "
    "companies, deals, decisions, facts, and stated preferences that outlive this "
    "session. You are memory for the USER, not a log of how you did your job.\n\n"
    "Do NOT record your own operational or harness mechanics. These are noise, not "
    "memory — skip them entirely, never mint a fact/insight/procedure for any of:\n"
    "- tool, skill, or policy internals (skill signing, tofu/trust errors, "
    "'signature invalidation', 'forbidden-composition policy gate', "
    "'create-skill signs once / edit invalidates');\n"
    "- turn, loop, or approval-gate conduct (how to take a turn, HumanGate/approval "
    "mechanics, what you're allowed to do this turn);\n"
    "- harness debugging or self-repair ('fix tofu: deny signature errors', retrying a "
    "denied call, wiring up a module);\n"
    "- anything about YOU (the agent) rather than the user's world.\n"
    "If a candidate reads like the agent narrating its own tooling, drop it.\n\n"
    "Process, using the tools:\n"
    "1. Read the episodes. Identify the durable facts, insights, and procedures ABOUT "
    "THE USER'S WORLD (apply the do-NOT-record filter above first).\n"
    "2. Before writing an entity, ALWAYS search_similar_entity / read_card first so a "
    "variant spelling folds onto the existing card instead of minting a duplicate.\n"
    "3. write_fact for each durable attribute; record_insight for real domain "
    "abstractions; record_procedure for repeatable how-tos the USER cares about.\n"
    "4. merge_entities when you find two cards for the same real-world thing; link "
    "related cards; set_alias so future writes fold correctly.\n"
    "5. Be NON-LOSSY and specific — many precise facts beat one vague sentence. "
    "Ground everything in the episodes; invent nothing.\n"
    "6. Be decisive: search only as much as you need, then WRITE. Don't spend the turn "
    "budget reading — prioritize write_fact / merge_entities / link over exploration.\n"
    "7. Stop when the window is fully consolidated. Do not loop."
)


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
        system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
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


__all__ = ["CONSOLIDATION_SYSTEM_PROMPT", "AgenticResult", "run_agentic_consolidation"]
