"""Per-agent memory_acl module runtime context.

The memory_acl hooks share state (config, telemetry). Decorator-stamped
hooks read that state lazily via :func:`state` after :func:`configure` is
called once at agent startup. State is bound to a
:class:`contextvars.ContextVar` (task 27/32 — a plain module global here
is silently overwritten by whichever agent's ``asyncio.Task`` most
recently called ``configure()``); see
``arcagent/builtins/capabilities/_runtime.py`` for the full rationale and
the reference pattern this module mirrors.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import Any

from arcagent.modules.memory_acl.config import MemoryACLConfig

_logger = logging.getLogger("arcagent.modules.memory_acl._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the three memory_acl hooks."""

    config: MemoryACLConfig
    telemetry: Any


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_memory_acl_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    cfg = MemoryACLConfig(**(config or {}))
    _state_var.set(_State(config=cfg, telemetry=telemetry))


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        msg = (
            "memory_acl module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
        raise RuntimeError(msg)
    return current


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["configure", "reset", "state"]
