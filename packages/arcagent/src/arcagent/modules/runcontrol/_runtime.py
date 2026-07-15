"""Per-agent run-control module runtime context.

The decorator-form module (``capabilities.py``) can't carry state in a closure —
``@background_task`` / ``@hook`` stamps wrap plain functions — so runtime state
(store, config, telemetry, identity, and the captured agent) lives on a
:class:`_State` bound to a :class:`contextvars.ContextVar`, configured at startup.

``configure()`` is SYNC (matching every module template) and does no I/O — it only
stores plain state; :func:`ensure_store`, awaited by the watcher on first use, does
the actual async backend open. A plain module global would be silently overwritten
by whichever agent's ``asyncio.Task`` last called ``configure()`` — see
``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.runcontrol.config import RuncontrolConfig
from arcagent.modules.runcontrol.store import open_store

if TYPE_CHECKING:
    from arctrust import AgentIdentity


@dataclass
class _State:
    """Mutable runtime state shared across the run-control module's callbacks."""

    config: RuncontrolConfig
    workspace: Path
    telemetry: Any
    identity: AgentIdentity
    # The live agent, captured from the ``agent:ready`` payload's bound ``run_fn``
    # (``run_fn.__self__``). The watcher reads the agent's tracked-run map to
    # resolve a cancel request to its live handle — arcagent exposes no public
    # enumerator over live runs, and its ``agent.py`` is owned elsewhere, so the
    # captured reference is the seam. None until agent:ready fires.
    agent: Any = None
    # Built lazily by ensure_store() on first watcher tick (mirrors tasks). None
    # means "not built yet".
    store: Any = None
    # Serialises the lazy first-use build so two watcher ticks can't both open the
    # backend (check-then-act race → one orphaned connection).
    init_lock: asyncio.Lock = None  # type: ignore[assignment]  # reason: set in __post_init__

    def __post_init__(self) -> None:
        # A mutable default (asyncio.Lock()) on the dataclass field would be shared
        # across every _State instance; build a fresh one per agent here.
        self.init_lock = asyncio.Lock()


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_runcontrol_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | RuncontrolConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    identity: AgentIdentity,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at startup.

    Synchronous by contract — no I/O happens here (see module docstring).
    """
    cfg = config if isinstance(config, RuncontrolConfig) else RuncontrolConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            telemetry=telemetry,
            identity=identity,
        )
    )


async def ensure_store() -> None:
    """Idempotent: open the arcstore ``cancellations`` backend once per agent.

    Mirrors tasks' ``ensure_store``. The watcher awaits this before touching
    ``st.store``; the build is serialised under ``init_lock`` (the re-check inside
    is the build-once guard) so two ticks can't both open the backend.
    """
    st = state()
    if st.store is not None:
        return
    async with st.init_lock:
        if st.store is not None:
            return
        st.store = await open_store(st.config.data_dir)


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "runcontrol module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()``. Called at the top of every turn-dispatch entry point
    (task 27 follow-up hotfix) so a callback running in a fresh sibling
    ``asyncio.Task`` still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "ensure_store", "reset", "state"]
