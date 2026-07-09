"""Thin skills wiring — the only arcagent-side improver code (SPEC-044).

This module holds no improvement logic; it forwards *primitive* per-turn signals to
the config-selected :class:`~arcagent.skilladapt.SkillAdapter`:

* ``agent:post_tool``   — detect a skill read (open the active span), then forward each
  subsequent tool call as ``observe`` (the signal-extraction half of the old
  ``trace_collector``);
* ``agent:post_plan``   — ``on_turn_end`` closes the span + accrues usage;
* ``agent:pre_respond`` — ``maybe_improve`` triggers the gated improvement pass;
* ``agent:ready``       — index skill paths from the registry.

With a :class:`~arcagent.skilladapt.NullSkillAdapter` selected, ``state().active`` is
``False`` and every hook short-circuits — a silent no-op that writes nothing (AC-1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcagent.modules.skills import _runtime
from arcagent.tools._decorator import hook

_logger = logging.getLogger("arcagent.modules.skills.capabilities")


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
    """Close the active span at turn end and clear the active-skill tracker."""
    st = _runtime.state()
    if not st.active:
        return
    outcome = str(ctx.data.get("task_outcome", ""))
    turn = int(ctx.data.get("turn_number", 0))
    await st.adapter.on_turn_end(turn=turn, outcome=outcome)
    st.active_skill = None


@hook(event="agent:pre_respond", priority=150)
async def skills_pre_respond(ctx: Any) -> None:
    """Trigger the gated improvement pass for over-threshold skills.

    ``insight`` (optional arcmemory recurring-failure abstraction, REQ-060) is passed
    memory-less here: no producer populates it at the ``agent:pre_respond`` emit, and
    wiring the active Brain's retrieval there costs core LOC the budget can't spare —
    so the extension calls ``maybe_improve()`` plainly (the fully-supported memory-less
    default). The arcskill consumer still accepts ``insight`` for a BYO producer or a
    later SPEC-047 wire; see the SPEC-044 README deviation.
    """
    del ctx
    st = _runtime.state()
    if not st.active:
        return
    await st.adapter.maybe_improve()


@hook(event="agent:ready", priority=100)
async def skills_ready(ctx: Any) -> None:
    """Index skill paths and start the Curator lifecycle sweep on the proactive engine."""
    st = _runtime.state()
    if not st.active:
        return
    _runtime.start_sweep()
    registry = ctx.data.get("skill_registry") or st.skill_registry
    if registry is None:
        _logger.warning("no skill_registry in agent:ready; skill trace attribution disabled")
        return
    st.index_skills(registry)


@hook(event="agent:shutdown", trylast=True)
async def skills_shutdown(ctx: Any) -> None:
    """Stop the lifecycle-sweep engine and drain in-flight sweeps on shutdown."""
    del ctx
    await _runtime.stop_sweep()


__all__ = [
    "skills_post_plan",
    "skills_post_tool",
    "skills_pre_respond",
    "skills_ready",
    "skills_shutdown",
]
