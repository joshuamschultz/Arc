"""Thin skills wiring — the only arcagent-side improver code (SPEC-044).

This module holds no improvement logic; it forwards *primitive* per-turn signals to
the config-selected :class:`~arcagent.skilladapt.SkillAdapter`:

* ``agent:post_tool``   — detect a skill read (open the active span), then forward each
  subsequent tool call as ``observe`` (the signal-extraction half of the old
  ``trace_collector``);
* ``agent:post_plan``   — ``on_turn_end`` closes the span + accrues usage, and stashes the
  turn number for the off-loop Curator sweep;
* ``agent:pre_respond`` — ``maybe_improve`` triggers the gated improvement pass, threading
  any recurring-failure ``insight`` the memory module produced this turn (REQ-060);
* ``agent:ready``       — index skill paths from the CapabilityRegistry and rehydrate the
  retire/revive suppression set;
* a ``@background_task`` loop — drives the Curator lifecycle sweep on a config cadence.

With a :class:`~arcagent.skilladapt.NullSkillAdapter` selected, ``state().active`` is
``False`` and every hook short-circuits — a silent no-op that writes nothing (AC-1).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from arcagent.modules.skills import _runtime
from arcagent.tools._decorator import background_task, hook

_logger = logging.getLogger("arcagent.modules.skills.capabilities")

# @background_task interval is metadata only; the loop owns its own sleep, reading the
# live cadence from config each cycle (default hourly).
_SWEEP_POLL_DEFAULT = 3_600.0


def _call_status(ctx: Any) -> tuple[str, str | None]:
    """Derive (status, error_type) from an EventContext result."""
    if getattr(ctx, "is_vetoed", False):
        return "vetoed", None
    result = ctx.data.get("result")
    if isinstance(result, Exception):
        return "error", type(result).__name__
    return "ok", None


@hook(event="agent:post_tool", priority=200)
async def skills_post_tool(ctx: Any) -> None:
    """Detect skill reads; forward subsequent tool calls to the adapter as observations."""
    st = _runtime.state()
    if not st.active:
        return
    tool = ctx.data.get("tool", "")

    if tool == "read":
        file_path = ctx.data.get("args", {}).get("file_path", "")
        if file_path:
            try:
                resolved = Path(file_path).resolve()
            except (ValueError, OSError):
                return
            skill_name = st.skill_paths.get(resolved)
            if skill_name is not None:
                st.active_skill = skill_name
        return

    if st.active_skill is not None and tool:
        status, error_type = _call_status(ctx)
        await st.adapter.observe(
            skill_name=st.active_skill,
            tool_name=tool,
            status=status,
            error_type=error_type,
        )


@hook(event="agent:post_plan", priority=200)
async def skills_post_plan(ctx: Any) -> None:
    """Close the active span at turn end, stash the turn, and clear the active-skill tracker."""
    st = _runtime.state()
    if not st.active:
        return
    outcome = str(ctx.data.get("task_outcome", ""))
    turn = int(ctx.data.get("turn_number", 0))
    _runtime.record_turn(turn)
    await st.adapter.on_turn_end(turn=turn, outcome=outcome)
    st.active_skill = None


@hook(event="agent:pre_respond", priority=150)
async def skills_pre_respond(ctx: Any) -> None:
    """Trigger the gated improvement pass for over-threshold skills.

    ``insight`` is the optional recurring-failure abstraction the memory module's
    ``agent:pre_respond`` hook (priority 100, runs first) placed on ``ctx.data`` from the
    active Brain's retrieval; empty when memory is off (the improver works memory-less).
    """
    st = _runtime.state()
    if not st.active:
        return
    insight = str(ctx.data.get("insight", ""))
    await st.adapter.maybe_improve(insight=insight)


@hook(event="agent:ready", priority=100)
async def skills_ready(ctx: Any) -> None:
    """Index skill paths from the CapabilityRegistry and rehydrate retire suppression."""
    st = _runtime.state()
    if not st.active:
        return
    registry = ctx.data.get("skill_registry") or st.skill_registry
    if registry is None:
        _logger.warning("no skill_registry in agent:ready; skill trace attribution disabled")
        return
    st.index_skills(registry)
    # Rehydrate: re-suppress skills retired in a prior session (read from the on-disk
    # candidate-store manifest) so retirement survives restart (HIGH-3).
    await _runtime.reconcile_suppression()


@background_task(name="skills_review_lifecycle_loop", interval=_SWEEP_POLL_DEFAULT)
async def skills_review_lifecycle_loop(_ctx: Any) -> None:
    """Curator: periodically sweep retire/revive through the adapter (CRITICAL-1 producer).

    The decorator interval is metadata; the loop owns its cadence, reading the live
    ``sweep_poll_seconds`` config each cycle. Mirrors the memory consolidate loop.
    """
    while True:
        try:
            await _runtime.run_lifecycle_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # reason: fail-open — a sweep error must never crash the agent
            _logger.warning("skills lifecycle sweep failed", exc_info=True)
        await asyncio.sleep(_poll_interval())


def _poll_interval() -> float:
    """The live sweep-poll cadence, or the default when the module is unconfigured."""
    try:
        return _runtime.state().sweep_poll_seconds
    except RuntimeError:
        return _SWEEP_POLL_DEFAULT


__all__ = [
    "skills_post_plan",
    "skills_post_tool",
    "skills_pre_respond",
    "skills_ready",
    "skills_review_lifecycle_loop",
]
