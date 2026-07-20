"""Per-agent browser module runtime context.

The browser module's @capability class and @tool functions share state
(CDP client, accessibility manager, config, bus, telemetry).
Decorator-stamped functions can't carry that state in a closure, so it
lives in a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale
and the reference pattern this module mirrors.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.modules.browser.backends.protocols import BrowserBackend, BrowserSession
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the browser capability + tools.

    ``backend``, ``cdp_client`` (the live session), and ``ax_manager`` are
    populated by the :class:`BrowserCapability` ``setup()`` lifecycle
    hook; tool functions read the session + AX manager lazily via
    :func:`state`. All three are ``None`` between ``configure()`` and
    ``setup()``. ``backend`` is retained so ``teardown()`` can release
    any remote resources it acquired.
    """

    config: BrowserConfig
    workspace: Path
    bus: Any
    telemetry: Any
    backend: BrowserBackend | None = None
    cdp_client: BrowserSession | None = None
    ax_manager: Any = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_browser_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | BrowserConfig | None = None,
    workspace: Path = Path("."),
    bus: Any = None,
    telemetry: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    Accepts either a raw dict (from arcagent.toml) or an already-built
    :class:`BrowserConfig`. The CDP client and AX manager are created
    lazily by :class:`BrowserCapability.setup` so configure() stays cheap
    and side-effect-free.
    """
    if isinstance(config, BrowserConfig):
        cfg = config
    else:
        cfg = BrowserConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            workspace=workspace.resolve(),
            bus=bus,
            telemetry=telemetry,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "browser module called before runtime is configured; "
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
