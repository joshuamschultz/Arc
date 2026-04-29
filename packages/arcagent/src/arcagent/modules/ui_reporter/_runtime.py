"""Per-agent ui_reporter module runtime context.

The ui_reporter module's hooks share state (WebSocket transport, agent
identity, sequence counter, config). Decorator-stamped functions can't
carry that state in a closure, so it lives in a module-level
:class:`_State` instance configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime`. Single-
agent-per-process is the assumption; this is shared mutable state for
one agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.modules.ui_reporter import UIReporterConfig

_logger = logging.getLogger("arcagent.modules.ui_reporter._runtime")

# Events from arcrun bridged as agent:pre_tool/post_tool etc.
# These map to UIEvent layer="run", not "agent".
_RUN_LAYER_SUFFIXES = frozenset(
    {
        "pre_tool",
        "post_tool",
        "pre_plan",
        "post_plan",
    }
)


@dataclass
class _State:
    """Mutable runtime state shared across ui_reporter hooks."""

    config: UIReporterConfig
    workspace: Path
    transport: Any
    agent_name: str
    agent_id: str
    source_id: str
    sequence: int = 0


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    workspace: Path = Path("."),
    transport: Any | None = None,
    agent_name: str = "",
    agent_id: str = "",
    source_id: str = "",
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = UIReporterConfig(**(config or {}))
    _state = _State(
        config=cfg,
        workspace=workspace.resolve(),
        transport=transport,
        agent_name=agent_name,
        agent_id=agent_id or agent_name,
        source_id=source_id,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "ui_reporter module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


def classify_layer(event: str) -> str:
    """Map a ModuleBus event name to a UIEvent layer."""
    if event.startswith("llm:"):
        return "llm"
    if event.startswith("schedule:"):
        return "scheduler"
    if event.startswith("capability:"):
        # capability lifecycle is part of the agent's own surface — not a
        # separate dashboard layer. UIEvent.layer is a closed Literal so
        # routing capability:* under "agent" keeps schema compatibility
        # without forcing a UIEvent migration.
        return "agent"
    if event.startswith("agent:"):
        suffix = event.split(":", 1)[1]
        if suffix in _RUN_LAYER_SUFFIXES:
            return "run"
        return "agent"
    return "agent"


def wrap_event(event: str, data: dict[str, Any]) -> dict[str, Any]:
    """Convert a ModuleBus event into a UIEvent-compatible dict.

    Increments the per-agent sequence counter so arcui can detect gaps
    on reconnect (UIEvent.sequence is required, monotonic per source).
    """
    st = state()
    layer = classify_layer(event)
    event_type = event.split(":", 1)[1] if ":" in event else event
    seq = st.sequence
    st.sequence += 1
    return {
        "layer": layer,
        "event_type": event_type,
        "agent_id": st.agent_id,
        "agent_name": st.agent_name,
        "source_id": st.source_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": dict(data),
        "sequence": seq,
    }


async def emit_to_arcui(event: str, data: dict[str, Any]) -> None:
    """Forward one bus event to the arcui WebSocket transport.

    No-op when the transport is absent (no UI running, probe failed, or
    config disabled). Validation errors and transport faults are logged
    and swallowed — agent operation must never depend on UI liveness.
    """
    st = state()
    if st.transport is None:
        return
    payload = wrap_event(event, data)
    try:
        from arcui.types import UIEvent

        ui_event = UIEvent(**payload)
        await st.transport.send_event(st.agent_id, ui_event)
    except Exception:
        _logger.debug("ui_reporter: send_event failed", exc_info=True)


__all__ = [
    "classify_layer",
    "configure",
    "emit_to_arcui",
    "reset",
    "state",
    "wrap_event",
]
