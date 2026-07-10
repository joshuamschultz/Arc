"""Per-agent telegram module runtime context.

The telegram module's tool, hooks, and background task share state
(the :class:`TelegramBot` instance, config, telemetry, workspace).
Decorator-stamped functions can't carry that state in a closure, so
it lives in a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full
rationale. Safe for the ``telegram_poll`` background task too: it's
spawned via ``asyncio.create_task()`` after ``configure()`` in the same
agent-startup task, so asyncio's automatic context-copy on task creation
gives it this agent's bot/credentials for its whole lifetime — this is
exactly the credential-bleed vector the live DGX incident demonstrated
for signing keys; the fix here closes the identical hole for Telegram
bot tokens.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.modules.telegram.bot import TelegramBot
from arcagent.modules.telegram.config import TelegramConfig

_logger = logging.getLogger("arcagent.modules.telegram._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across tool / hooks / poll task."""

    config: TelegramConfig
    workspace: Path
    telemetry: Any
    bot: TelegramBot


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_telegram_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    egress_proxy: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    Constructs the :class:`TelegramBot` but does not start polling —
    the ``telegram_poll`` background task drives bot lifecycle so the
    capability loader's drain-then-replace semantics (R-062) apply.

    ``egress_proxy`` is the agent's shared EgressProxy; outbound notifications
    are mediated through it (SPEC-038 REQ-031).
    """
    cfg = TelegramConfig(**(config or {}))
    ws = workspace.resolve()
    bot = TelegramBot(config=cfg, telemetry=telemetry, workspace=ws, egress=egress_proxy)
    _state_var.set(
        _State(
            config=cfg,
            workspace=ws,
            telemetry=telemetry,
            bot=bot,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "telegram module called before runtime is configured; "
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
