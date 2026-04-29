"""Per-agent planning module runtime context.

The planning module's hooks and tools share a single piece of state: the
path to the workspace and the derived tasks.json location. Decorator-
stamped functions can't carry that in a closure, so it lives in a
module-level :class:`_State` instance configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger("arcagent.modules.planning._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across planning hooks and tools."""

    workspace: Path
    tasks_path: Path
    agent_name: str


_state: _State | None = None


def configure(
    *,
    workspace: Path = Path("."),
    agent_name: str = "",
    config: dict[str, Any] | None = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    ws = workspace.resolve()
    _state = _State(
        workspace=ws,
        tasks_path=ws / "tasks.json",
        agent_name=agent_name,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "planning module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
