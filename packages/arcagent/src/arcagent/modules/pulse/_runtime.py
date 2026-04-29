"""Per-agent pulse module runtime context.

The decorator-form pulse module (``capabilities.py``) cannot carry state
in a closure — ``@hook`` and ``@capability`` stamps wrap plain functions
and the ``@capability`` class is instantiated by the loader with no
arguments. Runtime state (engine, config, workspace, telemetry, optional
agent_run_fn) therefore lives on a module-level :class:`_State` instance
configured by the agent at startup.

Mirrors :mod:`arcagent.modules.scheduler._runtime` and is consistent
with the single-agent-per-process model.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.pulse import PulseConfig

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.modules.pulse.engine import PulseEngine

_logger = logging.getLogger("arcagent.modules.pulse._runtime")

AgentRunFn = Callable[..., Awaitable[Any]]


@dataclass
class _State:
    """Mutable runtime state shared across the pulse capability + hooks."""

    config: PulseConfig
    workspace: Path
    telemetry: AgentTelemetry | None
    llm_config: Any
    agent_name: str
    bus: Any = None
    agent_run_fn: AgentRunFn | None = None
    engine: PulseEngine | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | PulseConfig | None = None,
    telemetry: AgentTelemetry | None = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
    bus: Any = None,
    agent_run_fn: AgentRunFn | None = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    if isinstance(config, PulseConfig):
        cfg = config
    else:
        cfg = PulseConfig(**(config or {}))
    _state = _State(
        config=cfg,
        workspace=workspace.resolve(),
        telemetry=telemetry,
        llm_config=llm_config,
        agent_name=agent_name,
        bus=bus,
        agent_run_fn=agent_run_fn,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "pulse module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["AgentRunFn", "configure", "reset", "state"]
