"""LLM model loading + ArcRun/ArcLLM event bridges.

Sibling of ``arcagent.core.agent``. Owns the lazy model loader that
wires a JSONLTraceStore + on_event bridge into ArcLLM, and the two
event bridges that map ArcRun events and ArcLLM TraceRecords onto the
ModuleBus.

Re-exported through ``arcagent.core.agent`` so existing imports
(``from arcagent.core.agent import create_arcrun_bridge,
   create_arcllm_bridge``) keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcrun import Event

from arcagent.core.config import ArcAgentConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.utils import load_eval_model

_logger = logging.getLogger("arcagent.model_manager")


def create_arcrun_bridge(
    bus: ModuleBus,
    *,
    model_id: str = "",
    agent_label: str = "",
) -> Callable[[Event], None]:
    """Create on_event callback for arcrun.run().

    Maps ArcRun lifecycle events to Module Bus events:
      tool.start  → agent:pre_tool
      tool.end    → agent:post_tool
      turn.start  → agent:pre_plan
      turn.end    → agent:post_plan

    llm.call is NOT mapped — the arcllm bridge emits llm:call_complete
    from TraceRecord with trace_id, bodies, and phase timings.

    ArcRun's on_event is synchronous (Callable[[Event], None]),
    so we schedule the async bus.emit via the running event loop.
    """
    _event_map = {
        "tool.start": "agent:pre_tool",
        "tool.end": "agent:post_tool",
        "turn.start": "agent:pre_plan",
        "turn.end": "agent:post_plan",
    }
    _pending: set[asyncio.Task[Any]] = set()

    def bridge(event: Event) -> None:
        bus_event = _event_map.get(event.type)
        if bus_event is not None:
            # Always copy to a plain dict — Event.data is typed as
            # MappingProxyType[Any, Any] (read-only) by arcrun; ModuleBus.emit
            # requires dict[str, Any]. Shallow copy is intentional here.
            data: dict[str, Any] = dict(event.data)
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, data))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            except RuntimeError:
                _logger.warning(
                    "No running event loop for bridge event: %s",
                    event.type,
                )

    return bridge


def create_arcllm_bridge(bus: ModuleBus) -> Callable[[Any], None]:
    """Create on_event callback for ArcLLM's load_model().

    Maps ArcLLM TraceRecord event_types to Module Bus events:
      llm_call       → llm:call_complete
      config_change  → llm:config_change
      circuit_change → llm:circuit_change

    ArcLLM's on_event is synchronous (Callable[[TraceRecord], None]),
    so we schedule the async bus.emit via the running event loop.
    Accepts both TraceRecord (Pydantic) and plain dict inputs.
    """
    _event_map = {
        "llm_call": "llm:call_complete",
        "config_change": "llm:config_change",
        "circuit_change": "llm:circuit_change",
    }
    # Hold strong references to pending tasks so they aren't GC'd
    _pending: set[asyncio.Task[Any]] = set()

    def bridge(record: Any) -> None:
        data = record.model_dump() if hasattr(record, "model_dump") else record
        event_type = data.get("event_type", "")
        bus_event = _event_map.get(event_type)
        if bus_event is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, data))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            except RuntimeError:
                _logger.warning(
                    "No running event loop for LLM bridge event: %s",
                    event_type,
                )

    return bridge


def ensure_model(
    *,
    config: ArcAgentConfig,
    workspace: Path,
    bus: ModuleBus | None,
) -> tuple[Any, Any]:
    """Load the eval model, wiring trace store + on_event bridge.

    Passes a JSONLTraceStore so every LLM call is persisted to
    ``<agent_root>/traces/`` for historical UI display and audit.
    Per ``arcllm.JSONLTraceStore`` (NIST AU-9), traces live OUTSIDE
    the workspace tool sandbox — the trace store wants the agent
    root, not the workspace subdirectory.

    Returns ``(model, trace_store)``. The caller is responsible for
    caching both — this helper is intentionally stateless so it can
    be unit-tested without an ArcAgent instance.
    """
    from arcllm.trace_store import JSONLTraceStore

    agent_root = workspace.parent
    trace_store = JSONLTraceStore(agent_root)
    on_event = create_arcllm_bridge(bus) if bus is not None else None
    model = load_eval_model(
        config.llm.model,
        trace_store=trace_store,
        agent_label=config.agent.name,
        on_event=on_event,
        arcllm_modules=config.llm.modules or None,
    )
    return model, trace_store
