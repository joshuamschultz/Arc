"""Per-agent policy module runtime context.

The policy module's three hooks share state (engine, session messages,
turn count, background tasks). Decorator-stamped functions can't carry
that state in a closure, so it lives in a :class:`_State` instance bound
to a :class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale
and the reference pattern this module mirrors.
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
from arcagent.modules.policy.config import PolicyConfig
from arcagent.modules.policy.policy_engine import PolicyEngine
from arcagent.utils.io import atomic_write_text

_logger = logging.getLogger("arcagent.modules.policy._runtime")

# Cadence counters persist here so a process restart resumes mid-cadence instead
# of resetting turn_count to 0 (the production box restarts every 1-5 minutes,
# which never let an in-memory counter reach eval_interval_turns).
_STATE_FILE = ".policy-state.json"


@dataclass
class _State:
    """Mutable runtime state shared across the three policy hooks."""

    config: PolicyConfig
    eval_config: EvalConfig
    workspace: Path
    telemetry: Any
    llm_config: Any
    engine: PolicyEngine
    eval_label: str
    eval_model: Any = None
    session_messages: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
    # Turn at which the consolidation-grounded reflection ("daily notes" eval)
    # last ran, so it fires on a turn cadence rather than every consolidation.
    last_reflect_turn: int = 0
    # Wall-clock time and turn_count at the last policy eval — persisted so the
    # idle-flush backstop accumulates across restarts.
    last_eval_ts: float = 0.0
    turns_at_last_eval: int = 0
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None

    def persist(self) -> None:
        """Atomically write the cadence counters so a restart resumes mid-cadence."""
        atomic_write_text(
            self.workspace / _STATE_FILE,
            json.dumps(
                {
                    "turn_count": self.turn_count,
                    "last_reflect_turn": self.last_reflect_turn,
                    "last_eval_ts": self.last_eval_ts,
                    "turns_at_last_eval": self.turns_at_last_eval,
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
    "arcagent_policy_state", default=None
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
    cfg = PolicyConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = workspace.resolve()
    persisted = _load_persisted(ws)
    new_state = _State(
        config=cfg,
        eval_config=ec,
        workspace=ws,
        telemetry=telemetry,
        llm_config=llm_config,
        engine=PolicyEngine(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            max_input_tokens=ec.max_input_tokens,
        ),
        eval_label=f"{agent_name}/eval" if agent_name else "eval",
        semaphore=asyncio.Semaphore(ec.max_concurrent),
        turn_count=int(persisted.get("turn_count", 0)),
        last_reflect_turn=int(persisted.get("last_reflect_turn", 0)),
        last_eval_ts=float(persisted.get("last_eval_ts", time.time())),
        turns_at_last_eval=int(persisted.get("turns_at_last_eval", 0)),
    )
    _state_var.set(new_state)
    if not persisted:
        # Seed the idle clock so it accumulates across restarts before the first eval.
        new_state.persist()


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "policy module called before runtime is configured; "
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
