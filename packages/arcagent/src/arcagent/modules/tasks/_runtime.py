"""Per-agent tasks module runtime context.

The decorator-form tasks module (``capabilities.py``) cannot carry state
in a closure — ``@tool`` stamps wrap plain functions — so runtime state
(store, config, telemetry, identity, registry) lives on a :class:`_State`
instance bound to a :class:`contextvars.ContextVar`, configured by the
agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.

Owner-only enforcement needs the agent's own identity, so — unlike the
scheduler template, which carries none — ``configure()`` mirrors the
messaging module's identity handling: it receives ``identity`` (the
agent's ``AgentIdentity``) and ``registry`` (arcteam's ``EntityRegistry``,
for ``@handle`` resolution) and stores both on ``_State`` (SDD §3).
``configure()`` is async, unlike the scheduler/messaging templates, because
opening the arcstore backend does real I/O (SQLite schema init).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.tasks.config import TasksConfig
from arcagent.modules.tasks.store import open_store

if TYPE_CHECKING:
    from arcstore.tasks import TaskStore
    from arcteam.registry import EntityRegistry
    from arctrust import AgentIdentity


@dataclass
class _State:
    """Mutable runtime state shared across the tasks module's tools."""

    config: TasksConfig
    workspace: Path
    telemetry: Any
    identity: AgentIdentity
    registry: EntityRegistry
    store: TaskStore


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_tasks_state", default=None
)


async def configure(
    *,
    config: dict[str, Any] | TasksConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
    registry: EntityRegistry,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    cfg = config if isinstance(config, TasksConfig) else TasksConfig(**(config or {}))
    ws = workspace.resolve()
    store = await open_store(cfg.data_dir)
    _state_var.set(
        _State(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            identity=identity,
            registry=registry,
            store=store,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "tasks module called before runtime is configured; "
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
