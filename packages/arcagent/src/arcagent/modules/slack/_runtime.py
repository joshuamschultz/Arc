"""Per-agent slack module runtime context.

The slack module's WebSocket lifecycle and three hooks all share the
same :class:`SlackBot` instance plus its config / workspace /
telemetry. Decorator-stamped functions can't carry that state in a
closure, so it lives in a module-level :class:`_State` instance
configured by the agent at startup.

Mirrors :mod:`arcagent.modules.policy._runtime`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.modules.slack.config import SlackConfig

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.modules.slack.bot import SlackBot

_logger = logging.getLogger("arcagent.modules.slack._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the slack capability + hooks."""

    config: SlackConfig
    workspace: Path
    telemetry: AgentTelemetry | None
    bot: SlackBot | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    workspace: Path = Path("."),
    telemetry: Any = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = SlackConfig(**(config or {}))
    _state = _State(
        config=cfg,
        workspace=workspace.resolve(),
        telemetry=telemetry,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "slack module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
