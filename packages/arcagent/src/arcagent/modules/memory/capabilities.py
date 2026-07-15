"""Thin memory wiring — the only arcagent-side memory code (SPEC-041 §4.6, DC-4).

This module holds no memory logic; it wires the config-selected
:class:`~arcagent.brain.Brain` to the agent lifecycle:

* ``capture`` on ``agent:post_tool`` + ``agent:post_respond`` (fast, zero-LLM);
* ``retrieve`` on ``agent:assemble_prompt`` @ priority 50 → ``sections["recall"]``,
  query-conditioned, once-per-turn (spawn double-assembly hits the cache);
* one de-duplicated ``memory_search`` tool;
* ``consolidate`` scheduled by a ``@background_task`` that polls an event-count /
  idle trigger (DC-5) and emits ``memory.consolidated`` for grounded reflection.

Every Brain call is preceded by a generic ACL check: the wiring asks the selected
Brain provider to :meth:`authorize` the operation and honors a denial before touching
the brain (the ACL *policy* lives in the backend; arcagent only asks). With a
:class:`~arcagent.brain.NullBrain` selected, ``state().active`` is ``False`` and every
hook short-circuits — a truly silent no-op that writes nothing.
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

# Told to "remember X" with a NullBrain active, the model will happily reply
# "saved to memory" while nothing persists (ASI09 trust exploitation). There is
# no save-tool to make truthful — capture is an automatic hook that silently
# no-ops — so the honest fix is to tell the model, in the prompt, that durable
# memory is off whenever the brain is inactive.
_MEMORY_DISABLED_NOTE = (
    "Durable memory is DISABLED for this agent. Nothing you are told to "
    "'remember', 'save', or 'note for later' persists beyond this session. Do "
    "not claim anything was saved to memory; say plainly that persistent memory "
    "is off."
)


# -- ACL gating ----------------------------------------------------------


async def _acl_allows(operation: str, caller_did: str) -> bool:
    """Ask the selected Brain provider to authorize a memory operation.

    The generic "ask the provider" seam: the ACL *policy* lives in the backend, so the
    wiring simply calls the Brain's optional ``authorize(operation, caller_did=...)``
    before recall or capture. A backend that exposes no such method (or the NullBrain)
    imposes no gate — allow.
    """
    st = _runtime.state()
    authorize = getattr(st.brain, "authorize", None)
    if authorize is None:
        return True
    return bool(await authorize(operation, caller_did=caller_did))


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


@hook(event="agent:assemble_prompt", priority=_RECALL_PRIORITY)
async def inject_memory_disabled_note(ctx: Any) -> None:
    """Tell the model durable memory is off when the brain is inactive (NullBrain).

    Only fires when the memory module is loaded but ``active`` is False (brain
    ``none`` — federal zero-config or explicit). Prevents the "saved to memory"
    over-claim. When a real brain is active this is silent.
    """
    st = _runtime.state()
    if st.active:
        return
    sections = ctx.data.get("sections")
    if isinstance(sections, dict):
        sections["memory_status"] = _MEMORY_DISABLED_NOTE


@hook(event="agent:pre_respond", priority=100)
async def inject_insight(ctx: Any) -> None:
    """Produce the skills-improver ``insight`` from Brain recall (SPEC-044 REQ-060 / MED-4).

    Runs before the skills reader (priority 150, lower runs first) and places Brain-derived
    recall text on ``ctx.data["insight"]`` so the improver's code/prose mutator gets
    grounded context. ACL-gated like every Brain read; empty/absent when memory is off, so
    the improver stays fully memory-less. (A narrower failure-only insight channel is a
    possible SPEC-047 follow-on.)
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


# Tool events that are pure transcript noise for durable memory. Recalling memory
# and re-capturing the recall is a feedback loop (the "untrusted reference DATA
# retrieved from memory" garbage), and trivial results ("No matches found.",
# "removed", "ok") carry no signal worth distilling.
_NO_CAPTURE_TOOLS = frozenset({"memory_search"})
_MIN_TOOL_RESULT_CHARS = 24


def _worth_capturing_tool(tool_name: str, result: str) -> bool:
    """Whether a tool event carries durable signal (vs. transcript noise)."""
    if tool_name in _NO_CAPTURE_TOOLS:
        return False
    stripped = result.strip()
    if len(stripped) < _MIN_TOOL_RESULT_CHARS:
        return False
    return "untrusted reference data retrieved from memory" not in stripped.lower()


@hook(event="agent:post_tool", priority=_CAPTURE_PRIORITY)
async def capture_tool(ctx: Any) -> None:
    """Capture a tool invocation + result (fast, zero-LLM) — skipping pure noise."""
    st = _runtime.state()
    if not st.active:
        return
    tool_name = str(ctx.data.get("tool", ""))
    result = str(ctx.data.get("result", ""))
    if not _worth_capturing_tool(tool_name, result):
        return
    text = f"tool:{tool_name} -> {result}".strip()
    await _capture(st, text, kind="tool")


@hook(event="agent:pre_respond", priority=_CAPTURE_PRIORITY)
async def capture_user(ctx: Any) -> None:
    """Capture the user's input turn (fast, zero-LLM).

    Without this, memory is built only from tool plumbing and the agent's own
    responses — the human's actual words never enter it. The task text is the
    external input driving the turn; captured as ``user`` so curation keeps it and
    distillation learns from what was asked. Sanitize/dedup happen in the Brain's
    fast path (untrusted input, LLM01), same as every other capture.
    """
    st = _runtime.state()
    if not st.active:
        return
    text = str(ctx.data.get("task", "")).strip()
    await _capture(st, text, kind="user")


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
    elapsed = clock - st.last_consolidate_at
    threshold_hit = st.events_since_consolidate >= st.config.consolidate_event_threshold
    idle_hit = idle >= st.config.consolidate_idle_seconds
    interval_hit = elapsed >= st.config.consolidate_interval_seconds
    if not (threshold_hit or idle_hit or interval_hit):
        return False

    result = await st.brain.consolidate()
    st.events_since_consolidate = 0
    st.last_consolidate_at = clock
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
    "capture_user",
    "consolidate_poll_once",
    "inject_insight",
    "inject_memory_disabled_note",
    "inject_recall",
    "memory_consolidate_loop",
    "memory_search",
]
