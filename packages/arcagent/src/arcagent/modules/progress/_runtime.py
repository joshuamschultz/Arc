"""Per-agent runtime state for the progress-narration module.

The decorator-form module (``capabilities.py``) cannot carry state in a closure —
``@hook`` stamps wrap plain functions — so the config, the gateway's channel
delivery callback, and the per-channel tally live on a :class:`_State` bound to a
:class:`contextvars.ContextVar`, configured once at startup. A plain module
global would be silently overwritten by whichever agent's ``asyncio.Task`` last
called ``configure()``; see ``arcagent/builtins/capabilities/_runtime.py`` for
the full rationale.

State here is per-agent, and within an agent it is keyed by CHANNEL. Two
conversations on two different channels each get their own tally and neither can
be counted into the other's "3 of 4 agents finished".
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.modules.progress.config import ProgressConfig


@dataclass
class Tally:
    """What one channel has been told about the run it is watching.

    ``started`` counts only the child agents already ANNOUNCED, so the batch
    result never claims a total the person was never told about. ``pending``
    holds the ones seen since the last announcement, waiting out the coalesce
    window in ``flush_task``.
    """

    lines_sent: int = 0
    last_sent: float = 0.0
    # What produced the last line we actually sent. The gap throttle only fires
    # when the same source speaks twice in a row.
    last_kind: str = ""
    started: int = 0
    ended: int = 0
    failed: int = 0
    pending: int = 0
    flush_task: asyncio.Task[None] | None = None
    # "Still working" heartbeat state for a plain (non-dynamic) run. ``run_started``
    # is the monotonic time the current run's strategy was picked; 0.0 means no run
    # is being tracked yet. ``suppress_heartbeat`` is set for the dynamic strategy,
    # which narrates its own stages and needs no heartbeat on top.
    run_started: float = 0.0
    last_heartbeat: float = 0.0
    suppress_heartbeat: bool = False


@dataclass
class _State:
    """Mutable runtime state shared across the progress module's hooks."""

    config: ProgressConfig
    workspace: Path
    telemetry: Any
    # Channel delivery ("platform:chat_id", text) -> None, handed over by the
    # embedded gateway at ``agent:ready``. None on a headless agent, which is
    # simply an agent that narrates nothing.
    channel_deliver_fn: Any = None
    # One tally per origin channel, dropped when that run's narration ends.
    tallies: dict[str, Tally] = field(default_factory=dict)
    # In-flight coalesce timers, cancelled at shutdown.
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_progress_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at startup."""
    _state_var.set(
        _State(
            config=ProgressConfig(**(config or {})),
            workspace=workspace.resolve(),
            telemetry=telemetry,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "progress module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()``. Called at the top of every turn-dispatch entry point
    so a hook running in a fresh sibling ``asyncio.Task`` still sees this agent's
    state rather than whichever agent configured last.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["Tally", "bind", "configure", "reset", "state"]
