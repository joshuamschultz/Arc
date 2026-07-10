"""Per-agent user_profile module runtime context.

The two hooks and the three tools share state (config, workspace, store,
telemetry). Decorator-stamped functions can't carry that state in a closure,
so it lives in a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.store import ProfileStore

_logger = logging.getLogger("arcagent.modules.user_profile._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across user_profile hooks and tools."""

    config: UserProfileConfig
    workspace: Path
    telemetry: Any
    store: ProfileStore
    agent_name: str


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_user_profile_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    agent_name: str = "",
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    cfg = UserProfileConfig(**(config or {}))
    ws = workspace.resolve()
    _state_var.set(
        _State(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            store=ProfileStore(ws, cfg, telemetry=telemetry),
            agent_name=agent_name,
        )
    )
    _logger.info(
        "user_profile module runtime configured workspace=%s profile_dir=%s",
        ws,
        cfg.profile_dir,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "user_profile module called before runtime is configured; "
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
