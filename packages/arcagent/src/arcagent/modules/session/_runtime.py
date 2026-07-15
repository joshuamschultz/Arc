"""Per-agent session module runtime context.

The decorator-form session capability (``capabilities.py``) cannot carry
state in a closure — ``@tool`` wraps a plain function and ``@capability``
is instantiated by the loader with no arguments. Runtime state (index,
identity_graph, config, workspace) therefore lives on a :class:`_State`
instance bound to a :class:`contextvars.ContextVar`, configured by the
agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
the embedded gateway runs many ``ArcAgent`` instances concurrently in one
process (``arcui.embedded_agents._BoundedAgentCache``), with
``SessionRouter.handle()`` spawning one task per session, so different
agents' turns interleave on the same event loop. A ``ContextVar`` gives
each task its own isolated value: ``configure()`` inside one agent's task
is invisible to a concurrently-running sibling task. See
``arcagent/builtins/capabilities/_runtime.py`` for the reference pattern
this module mirrors — same public API, ``global`` replaced by
``.set()``/``.get()``.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.modules.session.identity_graph import IdentityGraph
    from arcagent.modules.session.index import SessionIndex

_logger = logging.getLogger("arcagent.modules.session._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the session capability and tools."""

    workspace: Path
    poll_interval: float
    index: SessionIndex | None = field(default=None)
    identity_graph: IdentityGraph | None = field(default=None)


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_session_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    workspace: Path = Path("."),
    telemetry: Any = None,  # Reserved for future telemetry wiring; unused today.
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    ``telemetry`` is accepted but not yet wired — it mirrors the scheduler
    pattern so the call-site signature stays stable when telemetry support
    is added.
    """
    del telemetry  # Unused until telemetry wiring is implemented.
    from arcagent.modules.session.config import SessionConfig

    cfg = SessionConfig(**(config or {}))
    _state_var.set(
        _State(
            workspace=workspace.resolve(),
            poll_interval=cfg.poll_interval,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "session module called before runtime is configured; "
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


__all__ = ["bind", "configure", "reset", "state"]
