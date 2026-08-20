"""Per-agent scheduler module runtime context.

The decorator-form scheduler (``capabilities.py``) cannot carry state
in a closure — ``@tool`` and ``@hook`` stamps wrap plain functions, and
the ``@capability`` class is instantiated by the loader with no
arguments. Runtime state (engine, store, config, telemetry, optional
agent_run_fn) therefore lives on a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
Safe for the scheduler's own tick loop too: it's spawned via
``asyncio.create_task()`` after ``configure()`` in the same agent-startup
task, so asyncio's automatic context-copy on task creation gives it this
agent's state for its whole lifetime.
"""

from __future__ import annotations

import contextvars
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
    # This agent's team handle, used to scope workflow-trigger backfill to the
    # workflows THIS agent owns — deployment-wide workflows would otherwise be
    # scheduled by every agent and fire N times a night.
    agent_name: str = ""
    bus: Any = None
    agent_run_fn: AgentRunFn | None = None
    channel_deliver_fn: Callable[[str, str], Awaitable[None]] | None = None
    engine: SchedulerEngine | None = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_scheduler_state", default=None
)

# Run callbacks bound by ``agent:ready``, keyed by the agent's workspace.
#
# The state above is per-ASYNCIO-TASK, and the two halves of this binding do not
# always run in the same one: the capability builds the engine in the task that
# configured the module, while ``agent:ready`` fires wherever the agent started.
# When they differ, the hook set a callback on a state the engine could not see
# and the engine waited forever for a callback that had already arrived.
#
# The workspace path is the agent's identity for this purpose: both halves know
# it, it is stable across restarts, and it is per-agent — a fleet keeps one
# entry per agent rather than one for the process.
_bound_run_fns: dict[str, AgentRunFn] = {}


def remember_run_fn(workspace: Path | str, fn: AgentRunFn) -> None:
    """Record a callback so an engine in another task can still find it."""
    _bound_run_fns[str(workspace)] = fn


def recall_run_fn(workspace: Path | str) -> AgentRunFn | None:
    """The callback bound for this agent, whichever task bound it."""
    return _bound_run_fns.get(str(workspace))


def configure(
    *,
    config: dict[str, Any] | SchedulerConfig | None = None,
    telemetry: AgentTelemetry,
    workspace: Path = Path("."),
    agent_name: str = "",
    bus: Any = None,
    agent_run_fn: AgentRunFn | None = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    ``agent_run_fn`` is the agent's own run callback, handed in here rather than
    delivered later by an ``agent:ready`` event. An event-delivered binding can
    be missed — wrong task, wrong ordering, a handler that never ran — and when
    it is missed the engine has no way to tell, so every stored schedule sits
    due and silent. Handed in at configure time it cannot be missed.
    """
    if isinstance(config, SchedulerConfig):
        cfg = config
    else:
        cfg = SchedulerConfig(**(config or {}))
    ws = workspace.resolve()
    _state_var.set(
        _State(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            store=ScheduleStore(ws / cfg.store_path),
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
            "scheduler module called before runtime is configured; "
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


def forget_run_fns() -> None:
    """Test-only: drop every remembered callback."""
    _bound_run_fns.clear()


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = [
    "AgentRunFn",
    "bind",
    "configure",
    "forget_run_fns",
    "recall_run_fn",
    "remember_run_fn",
    "reset",
    "state",
]
