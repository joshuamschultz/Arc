"""Per-agent slack module runtime context.

The slack module's WebSocket lifecycle and three hooks all share the
same :class:`SlackBot` instance plus its config / workspace /
telemetry. Decorator-stamped functions can't carry that state in a
closure, so it lives in a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.
Safe for the WebSocket read loop too: ``SlackCapability.setup()`` calls
``configure()`` (or it has already run via ``configure_module_runtimes``)
before spawning the bot's background task in the SAME coroutine chain, so
``asyncio.create_task()``'s automatic context-copy captures this agent's
state into the read loop's own isolated context for its whole lifetime.
"""

from __future__ import annotations

import contextvars
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
    egress: Any = None
    bot: SlackBot | None = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_slack_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    workspace: Path = Path("."),
    telemetry: Any = None,
    egress_proxy: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    ``egress_proxy`` is the agent's shared EgressProxy; outbound notifications
    are mediated through it (SPEC-038 REQ-031).
    """
    cfg = SlackConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            telemetry=telemetry,
            egress=egress_proxy,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "slack module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["configure", "reset", "state"]
