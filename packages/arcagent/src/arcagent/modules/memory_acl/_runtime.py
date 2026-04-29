"""Per-agent memory_acl module runtime context.

The memory_acl hooks share state (config, telemetry, identity-bound
:class:`CapabilityStore`). Decorator-stamped hooks read that state
lazily via :func:`state` after :func:`configure` is called once at
agent startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.modules.memory._runtime`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from arcagent.modules.memory_acl.capability_tokens import CapabilityStore
from arcagent.modules.memory_acl.config import MemoryACLConfig

_logger = logging.getLogger("arcagent.modules.memory_acl._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the three memory_acl hooks."""

    config: MemoryACLConfig
    telemetry: Any
    identity: Any
    capability_store: CapabilityStore


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    identity: Any = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = MemoryACLConfig(**(config or {}))
    _state = _State(
        config=cfg,
        telemetry=telemetry,
        identity=identity,
        capability_store=CapabilityStore(identity=identity),
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        msg = (
            "memory_acl module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
        raise RuntimeError(msg)
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
