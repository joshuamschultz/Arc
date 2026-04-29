"""Per-agent browser module runtime context.

The browser module's @capability class and @tool functions share state
(CDP client, accessibility manager, config, bus, telemetry).
Decorator-stamped functions can't carry that state in a closure, so it
lives in a module-level :class:`_State` instance configured by the agent
at startup.

This mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
is consistent with the single-agent-per-process model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.modules.browser.cdp_client import CDPClientManager
from arcagent.modules.browser.config import BrowserConfig

_logger = logging.getLogger("arcagent.modules.browser._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the browser capability + tools.

    ``cdp_client`` and ``ax_manager`` are populated by the
    :class:`BrowserCapability` ``setup()`` lifecycle hook; tool functions
    read them lazily via :func:`state`. Both are ``None`` between
    ``configure()`` and ``setup()``.
    """

    config: BrowserConfig
    workspace: Path
    bus: Any
    telemetry: Any
    cdp_client: CDPClientManager | None = None
    ax_manager: Any = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | BrowserConfig | None = None,
    workspace: Path = Path("."),
    bus: Any = None,
    telemetry: Any = None,
) -> None:
    """Bind module state. Called once at agent startup.

    Accepts either a raw dict (from arcagent.toml) or an already-built
    :class:`BrowserConfig`. The CDP client and AX manager are created
    lazily by :class:`BrowserCapability.setup` so configure() stays cheap
    and side-effect-free.
    """
    global _state
    if isinstance(config, BrowserConfig):
        cfg = config
    else:
        cfg = BrowserConfig(**(config or {}))
    _state = _State(
        config=cfg,
        workspace=workspace.resolve(),
        bus=bus,
        telemetry=telemetry,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "browser module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
