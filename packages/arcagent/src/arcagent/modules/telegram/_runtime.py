"""Per-agent telegram module runtime context.

The telegram module's tool, hooks, and background task share state
(the :class:`TelegramBot` instance, config, telemetry, workspace).
Decorator-stamped functions can't carry that state in a closure, so
it lives in a module-level :class:`_State` instance configured by the
agent at startup.

Mirrors :mod:`arcagent.modules.policy._runtime` and is consistent
with the single-agent-per-process model.
"""

from __future__ import annotations

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


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
) -> None:
    """Bind module state. Called once at agent startup.

    Constructs the :class:`TelegramBot` but does not start polling —
    the ``telegram_poll`` background task drives bot lifecycle so the
    capability loader's drain-then-replace semantics (R-062) apply.
    """
    global _state
    cfg = TelegramConfig(**(config or {}))
    ws = workspace.resolve()
    bot = TelegramBot(config=cfg, telemetry=telemetry, workspace=ws)
    _state = _State(
        config=cfg,
        workspace=ws,
        telemetry=telemetry,
        bot=bot,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "telegram module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
