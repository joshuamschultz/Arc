"""Thin memory wiring — the only arcagent-side memory code (SPEC-041 §4.6, DC-4).

This module holds no memory logic; it wires the config-selected
:class:`~arcagent.brain.Brain` to the agent lifecycle:

* ``capture`` on ``agent:post_tool`` + ``agent:post_respond`` (fast, zero-LLM);
* ``retrieve`` on ``agent:assemble_prompt`` @ priority 50 → ``sections["recall"]``,
  query-conditioned, once-per-turn (spawn double-assembly hits the cache);
* one de-duplicated ``memory_search`` tool;
* ``consolidate`` scheduled by a ``@background_task`` that polls an event-count /
  idle trigger (DC-5) and emits ``memory.consolidated`` for grounded reflection.

Every Brain call is preceded by the priority-10 ``memory_acl`` veto: the wiring
emits the matching ``memory.search`` / ``memory.write`` bus event and honors a
veto before touching the brain (T-083). With a :class:`~arcagent.brain.NullBrain`
selected, ``state().active`` is ``False`` and every hook short-circuits — a truly
silent no-op that writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from arcagent.modules.memory import _runtime
from arcagent.tools._decorator import background_task, hook, tool
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.memory.capabilities")

_RECALL_PRIORITY = 50
_CAPTURE_PRIORITY = 100
_CONSOLIDATE_POLL_INTERVAL = 300.0


# -- ACL gating ----------------------------------------------------------


async def _acl_allows(operation: str, caller_did: str) -> bool:
    """Emit the memory_acl event at priority 10; return False if it vetoes.

    The wiring routes every Brain read/write through the ``memory.search`` /
    ``memory.write`` bus events so the priority-10 ``memory_acl`` guard fires
    *before* recall or capture. No bus (unit context) → allow.
    """
    st = _runtime.state()
    if st.bus is None:
        return True
    ctx = await st.bus.emit(
        operation,
        {"caller_did": caller_did, "target_user_did": "", "owner_did": caller_did},
        agent_did=st.agent_did,
    )
    return not ctx.is_vetoed


async def _audit(event: str, detail: dict[str, Any]) -> None:
    await safe_audit(_runtime.state().telemetry, event, detail, logger=_logger)


# -- Recall hook ---------------------------------------------------------


@hook(event="agent:assemble_prompt", priority=_RECALL_PRIORITY)
async def inject_recall(ctx: Any) -> None:
    """Query-conditioned recall into ``sections["recall"]`` (once per turn)."""
    st = _runtime.state()
    if not st.active:
        return
    sections = ctx.data.get("sections")
    if not isinstance(sections, dict):
        return
    query = (ctx.data.get("query") or "").strip()
    if not query:
        return

    key = hash(query)
    if key in st.recall_cache:
        text = st.recall_cache[key]
    else:
        if not await _acl_allows("memory.search", st.agent_did):
            return
        # Reuse the turn's existing abstraction (no new LLM call — OQ-1); the Brain
        # derives the structural cue seeds from its own entity/cue graph, so a
        # different-domain turn can still match a stored abstraction. ``summary`` is
        # empty unless a prior handler supplied one — a Brain that ignores it degrades
        # to lexical-only, never errors.
        summary = str(ctx.data.get("summary") or "")
        text = await st.brain.retrieve(
            query,
            clearance="unclassified",
            top_k=st.config.top_k,
            budget=st.config.budget,
            summary=summary,
        )
        _cache_recall(st, key, text)
        await _audit("memory.recall", {"query_len": len(query), "hit": bool(text)})

    if text:
        sections["recall"] = text


def _cache_recall(st: _runtime._State, key: int, text: str) -> None:
    """Bounded once-per-turn recall cache (FIFO eviction)."""
    if len(st.recall_cache) >= _runtime._RECALL_CACHE_CAP:
        st.recall_cache.pop(next(iter(st.recall_cache)))
    st.recall_cache[key] = text


@hook(event="agent:pre_respond", priority=100)
async def inject_insight(ctx: Any) -> None:
    """Produce the skills-improver ``insight`` from Brain recall (SPEC-044 REQ-060 / MED-4).

    Runs before the skills reader (priority 150, lower runs first) and places Brain-derived
    recall text on ``ctx.data["insight"]`` so the improver's code/prose mutator gets
    grounded context. ACL-gated like every Brain read; empty/absent when memory is off, so
    the improver stays fully memory-less. (A narrower failure-only insight channel is a
    possible arcmemory/SPEC-047 follow-on.)
    """
    st = _runtime.state()
    if not st.active:
        return
    query = str(ctx.data.get("task") or "").strip()
    if not query:
        return
    if not await _acl_allows("memory.search", st.agent_did):
        return
    text = await st.brain.retrieve(
        query, clearance="unclassified", top_k=st.config.top_k, budget=st.config.budget
    )
    if text:
        ctx.data["insight"] = text


# -- Capture hooks -------------------------------------------------------


@hook(event="agent:post_tool", priority=_CAPTURE_PRIORITY)
async def capture_tool(ctx: Any) -> None:
    """Capture a tool invocation + result (fast, zero-LLM)."""
    st = _runtime.state()
    if not st.active:
        return
    tool_name = ctx.data.get("tool", "")
    result = ctx.data.get("result", "")
    text = f"tool:{tool_name} -> {result}".strip()
    await _capture(st, text, kind="tool")


@hook(event="agent:post_respond", priority=_CAPTURE_PRIORITY)
async def capture_respond(ctx: Any) -> None:
    """Capture the assistant's response turn (fast, zero-LLM)."""
    st = _runtime.state()
    if not st.active:
        return
    messages = ctx.data.get("messages", [])
    text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict)).strip()
    await _capture(st, text, kind="respond")


async def _capture(st: _runtime._State, text: str, *, kind: str) -> None:
    """ACL-gated Brain capture + consolidation-trigger bookkeeping."""
    if not text:
        return
    if not await _acl_allows("memory.write", st.agent_did):
        return
    await st.brain.capture(text, kind=kind)
    st.events_since_consolidate += 1
    st.last_activity = time.monotonic()
    await _audit("memory.capture", {"kind": kind})


# -- memory_search tool --------------------------------------------------


@tool(
    name="memory_search",
    description="Search agent memory across observations, entities, and insights.",
    classification="read_only",
    when_to_use="Recall facts, past conversations, procedures, or learned insights.",
)
async def memory_search(query: str, top_k: int = 5) -> str:
    """Query-conditioned recall, boundary-marked (LLM01). Empty when memory is off."""
    st = _runtime.state()
    if not st.active:
        return "Memory is not enabled for this agent."
    if not await _acl_allows("memory.search", st.agent_did):
        return "No memory results found."
    text = await st.brain.retrieve(query, clearance="unclassified", top_k=top_k)
    await _audit("memory.recall", {"query_len": len(query), "hit": bool(text), "tool": True})
    return text or "No memory results found."


# -- Consolidation scheduler ---------------------------------------------


@background_task(name="memory_consolidate_loop", interval=_CONSOLIDATE_POLL_INTERVAL)
async def memory_consolidate_loop(_ctx: Any) -> None:
    """Poll the event-count / idle trigger; consolidate when it fires (DC-5)."""
    while True:
        try:
            await consolidate_poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a sleep-path error must not crash the agent
            _logger.warning("memory consolidation poll failed", exc_info=True)
        await asyncio.sleep(_CONSOLIDATE_POLL_INTERVAL)


async def consolidate_poll_once(*, now: float | None = None) -> bool:
    """Run one consolidation iff the trigger fired; return whether it ran.

    Trigger (DC-5): accumulated capture events cross ``consolidate_event_threshold``,
    or events are pending and the agent has been idle past ``consolidate_idle_seconds``.
    """
    st = _runtime.state()
    if not st.active or st.events_since_consolidate <= 0:
        return False
    clock = time.monotonic() if now is None else now
    idle = clock - st.last_activity
    threshold_hit = st.events_since_consolidate >= st.config.consolidate_event_threshold
    idle_hit = idle >= st.config.consolidate_idle_seconds
    if not (threshold_hit or idle_hit):
        return False

    result = await st.brain.consolidate()
    st.events_since_consolidate = 0
    await _audit("memory.consolidated", {"summary": str(result.get("episode_summary", ""))})
    if st.bus is not None:
        await st.bus.emit(
            "memory.consolidated",
            {
                "episode_summary": str(result.get("episode_summary", "")),
                "insights_minted": result.get("insights_minted", 0),
                "facts_updated": result.get("facts_updated", 0),
            },
            agent_did=st.agent_did,
        )
    return True


__all__ = [
    "capture_respond",
    "capture_tool",
    "consolidate_poll_once",
    "inject_insight",
    "inject_recall",
    "memory_consolidate_loop",
    "memory_search",
]
