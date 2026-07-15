"""Per-agent pulse module runtime context.

The decorator-form pulse module (``capabilities.py``) cannot carry state
in a closure — ``@hook`` and ``@capability`` stamps wrap plain functions
and the ``@capability`` class is instantiated by the loader with no
arguments. Runtime state (engine, config, workspace, telemetry, optional
agent_run_fn) therefore lives on a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale
and the reference pattern this module mirrors.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.pulse.config import PulseConfig

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


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_pulse_state", default=None
)


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
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    if isinstance(config, PulseConfig):
        cfg = config
    else:
        cfg = PulseConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            telemetry=telemetry,
            llm_config=llm_config,
            agent_name=agent_name,
            bus=bus,
            agent_run_fn=agent_run_fn,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "pulse module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call, no construction. Called at the top of
    every turn-dispatch entry point (task 27 follow-up hotfix) so a turn
    running in a fresh sibling ``asyncio.Task`` — not a descendant of the
    task that ran ``configure()`` — still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["AgentRunFn", "bind", "configure", "reset", "state"]
