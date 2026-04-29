"""Decorator-form ui_reporter module — SPEC-021 task 3.8.

Per-event ``@hook`` functions that mirror :class:`UIReporterModule`'s
``startup`` subscriptions. Each hook delegates to a single
:func:`arcagent.modules.ui_reporter._runtime.emit_to_arcui` helper so the
fan-in to the WebSocket transport stays a single emission point — the
hooks themselves are just decorator-stamped subscription declarations.

Subscribed events fall into four classes (R-002, R-050):

  * ``agent:*``       — lifecycle, prompt assembly, tool/plan bridge from arcrun
  * ``llm:*``         — call completion, config / circuit changes
  * ``schedule:*``    — cron fire results
  * ``capability:*``  — SPEC-021 lifecycle (added / removed / replaced /
                        registration_failed / setup_failed)

State (transport, agent identity, sequence) is shared via
:mod:`arcagent.modules.ui_reporter._runtime`. The agent configures it
once at startup; the hooks read state lazily.

The legacy :class:`UIReporterModule` class still exists alongside this
module to keep its existing test surface working; both forms route to
arcui via the same :class:`WebSocketTransport` instance internally.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.ui_reporter import _runtime
from arcagent.tools._decorator import hook

_logger = logging.getLogger("arcagent.modules.ui_reporter.capabilities")

# Priority 200 = observational tier. Business-logic hooks (policy,
# security) run first; ui_reporter only mirrors the resulting state to
# the dashboard, never participates in a veto.
_OBSERVATIONAL_PRIORITY = 200


# --- LLM events -----------------------------------------------------------


@hook(event="llm:call_complete", priority=_OBSERVATIONAL_PRIORITY)
async def on_llm_call_complete(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="llm:config_change", priority=_OBSERVATIONAL_PRIORITY)
async def on_llm_config_change(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="llm:circuit_change", priority=_OBSERVATIONAL_PRIORITY)
async def on_llm_circuit_change(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


# --- Agent lifecycle / orchestration events -------------------------------


@hook(event="agent:init", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_init(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:ready", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_ready(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:shutdown", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_shutdown(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:pre_respond", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_pre_respond(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:post_respond", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_post_respond(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:error", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_error(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:extensions_loaded", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_extensions_loaded(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:skills_loaded", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_skills_loaded(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:tools_reloaded", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_tools_reloaded(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:pre_tool", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_pre_tool(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:post_tool", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_post_tool(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:pre_plan", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_pre_plan(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:post_plan", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_post_plan(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="agent:pre_compaction", priority=_OBSERVATIONAL_PRIORITY)
async def on_agent_pre_compaction(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


# --- Scheduler events -----------------------------------------------------


@hook(event="schedule:completed", priority=_OBSERVATIONAL_PRIORITY)
async def on_schedule_completed(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="schedule:failed", priority=_OBSERVATIONAL_PRIORITY)
async def on_schedule_failed(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


# --- Capability lifecycle events (SPEC-021 R-050) -------------------------


@hook(event="capability:added", priority=_OBSERVATIONAL_PRIORITY)
async def on_capability_added(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="capability:removed", priority=_OBSERVATIONAL_PRIORITY)
async def on_capability_removed(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="capability:replaced", priority=_OBSERVATIONAL_PRIORITY)
async def on_capability_replaced(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="capability:registration_failed", priority=_OBSERVATIONAL_PRIORITY)
async def on_capability_registration_failed(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)


@hook(event="capability:setup_failed", priority=_OBSERVATIONAL_PRIORITY)
async def on_capability_setup_failed(ctx: Any) -> None:
    await _runtime.emit_to_arcui(ctx.event, ctx.data)
