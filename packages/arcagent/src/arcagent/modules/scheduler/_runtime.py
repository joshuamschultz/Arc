"""Per-agent scheduler module runtime context.

The decorator-form scheduler (``capabilities.py``) cannot carry state
in a closure — ``@tool`` and ``@hook`` stamps wrap plain functions, and
the ``@capability`` class is instantiated by the loader with no
arguments. Runtime state (engine, store, config, telemetry, optional
agent_run_fn) therefore lives on a module-level :class:`_State`
instance configured by the agent at startup.

This mirrors :mod:`arcagent.modules.policy._runtime` and is consistent
with the single-agent-per-process model.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.store import ScheduleStore

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.modules.scheduler.scheduler import SchedulerEngine

_logger = logging.getLogger("arcagent.modules.scheduler._runtime")

AgentRunFn = Callable[..., Awaitable[Any]]


@dataclass
class _State:
    """Mutable runtime state shared across the scheduler capability + tools."""

    config: SchedulerConfig
    workspace: Path
    telemetry: AgentTelemetry
    store: ScheduleStore
    bus: Any = None
    agent_run_fn: AgentRunFn | None = None
    engine: SchedulerEngine | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | SchedulerConfig | None = None,
    telemetry: AgentTelemetry,
    workspace: Path = Path("."),
    bus: Any = None,
    agent_run_fn: AgentRunFn | None = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    if isinstance(config, SchedulerConfig):
        cfg = config
    else:
        cfg = SchedulerConfig(**(config or {}))
    ws = workspace.resolve()
    _state = _State(
        config=cfg,
        workspace=ws,
        telemetry=telemetry,
        store=ScheduleStore(ws / cfg.store_path),
        bus=bus,
        agent_run_fn=agent_run_fn,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "scheduler module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["AgentRunFn", "configure", "reset", "state"]
