"""Per-agent runtime state for the workpad (self-managing ``context.md``) module.

The single ``agent:post_respond`` hook shares state — the eval config/model, the
run counter, the accumulated recent transcript, and the background-task set — via
a :class:`_State` bound to a :class:`contextvars.ContextVar`. Mirrors
:mod:`arcagent.modules.policy._runtime` exactly.

Task 27/32: a plain module global here would be silently overwritten by whichever
agent's ``asyncio.Task`` most recently called ``configure()``. The agent binds the
built state into every turn-dispatch task via :func:`bind` (see
``activate_runtime_bindings``), so a hook running in a fresh sibling task still
sees this agent's state.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.modules.workpad.config import WorkpadConfig
from arcagent.utils.io import atomic_write_text

_logger = logging.getLogger("arcagent.modules.workpad._runtime")

# Cadence counters persist here so a process restart resumes mid-cadence instead
# of resetting run_count to 0 (the production box restarts every 1-5 minutes,
# which never let an in-memory counter reach every_n_runs).
_STATE_FILE = ".workpad-state.json"


@dataclass
class _State:
    """Mutable runtime state shared across the workpad hook + maintainer."""

    config: WorkpadConfig
    eval_config: EvalConfig
    workspace: Path
    telemetry: Any
    llm_config: Any
    eval_label: str
    eval_model: Any = None
    run_count: int = 0
    # Wall-clock time and run_count at the last maintenance trigger — persisted so
    # the idle-flush backstop accumulates across restarts.
    last_maintenance_ts: float = 0.0
    runs_at_last_maintenance: int = 0
    # Recent role-tagged activity lines accumulated since the last rewrite; drained
    # (snapshotted + cleared) when the maintainer fires. Bounded by config.
    transcript: list[str] = field(default_factory=list)
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None

    def persist(self) -> None:
        """Atomically write the cadence counters so a restart resumes mid-cadence."""
        atomic_write_text(
            self.workspace / _STATE_FILE,
            json.dumps(
                {
                    "run_count": self.run_count,
                    "last_maintenance_ts": self.last_maintenance_ts,
                    "runs_at_last_maintenance": self.runs_at_last_maintenance,
                }
            ),
        )


def _load_persisted(workspace: Path) -> dict[str, Any]:
    """Read the persisted cadence counters; tolerate a missing/corrupt file."""
    try:
        data = json.loads((workspace / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_workpad_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    cfg = WorkpadConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = Path(workspace).resolve()
    persisted = _load_persisted(ws)
    new_state = _State(
        config=cfg,
        eval_config=ec,
        workspace=ws,
        telemetry=telemetry,
        llm_config=llm_config,
        eval_label=f"{agent_name}/workpad" if agent_name else "workpad",
        semaphore=asyncio.Semaphore(ec.max_concurrent),
        run_count=int(persisted.get("run_count", 0)),
        last_maintenance_ts=float(persisted.get("last_maintenance_ts", time.time())),
        runs_at_last_maintenance=int(persisted.get("runs_at_last_maintenance", 0)),
    )
    _state_var.set(new_state)
    if not persisted:
        # Seed the idle clock so it accumulates across restarts before the first flush.
        new_state.persist()
    _logger.info("workpad module configured (every_n_runs=%d)", cfg.every_n_runs)


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "workpad module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call. Called at the top of every turn-dispatch entry
    point so a turn running in a fresh sibling ``asyncio.Task`` still sees this
    agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "reset", "state"]
