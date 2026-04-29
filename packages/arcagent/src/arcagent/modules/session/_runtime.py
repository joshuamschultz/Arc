"""Per-agent session module runtime context.

The decorator-form session capability (``capabilities.py``) cannot carry
state in a closure — ``@tool`` wraps a plain function and ``@capability``
is instantiated by the loader with no arguments. Runtime state (index,
identity_graph, config, workspace) therefore lives on a module-level
:class:`_State` instance configured by the agent at startup.

This mirrors :mod:`arcagent.modules.scheduler._runtime` and is consistent
with the single-agent-per-process model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.modules.session.identity_graph import IdentityGraph
    from arcagent.modules.session.index import SessionIndex

_logger = logging.getLogger("arcagent.modules.session._runtime")

# Default poll interval (seconds). Tests override via config.
_DEFAULT_POLL_INTERVAL = 30.0


@dataclass
class _State:
    """Mutable runtime state shared across the session capability and tools."""

    workspace: Path
    poll_interval: float
    index: SessionIndex | None = field(default=None)
    identity_graph: IdentityGraph | None = field(default=None)


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    workspace: Path = Path("."),
    telemetry: Any = None,  # Reserved for future telemetry wiring; unused today.
) -> None:
    """Bind module state. Called once at agent startup.

    ``telemetry`` is accepted but not yet wired — it mirrors the scheduler
    pattern so the call-site signature stays stable when telemetry support
    is added.
    """
    global _state
    del telemetry  # Unused until telemetry wiring is implemented.
    cfg = config or {}
    poll_interval = float(cfg.get("poll_interval", _DEFAULT_POLL_INTERVAL))
    _state = _State(
        workspace=workspace.resolve(),
        poll_interval=poll_interval,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "session module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
