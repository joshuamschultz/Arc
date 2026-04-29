"""Per-agent delegate module runtime context.

The ``@tool``-decorated ``delegate`` function in ``capabilities.py`` cannot
capture its parent-tool list and signing key in a constructor. That state
lives here as a module-level :class:`_State` instance, configured by the
agent at startup.

Usage::

    # agent startup
    from arcagent.modules.delegate import _runtime
    _runtime.configure(
        parent_tools=agent.tools,
        parent_sk_bytes=agent.identity.sk_bytes,
        config=DelegateConfig.for_tier(agent.tier),
    )

This mirrors the pattern in :mod:`arcagent.builtins.capabilities._runtime`
and is consistent with the single-agent-per-process model.
"""

from __future__ import annotations

# TYPE_CHECKING guard avoids a hard import of arcrun at module load time,
# keeping cold-start latency low.
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arcagent.modules.delegate.config import DelegateConfig

if TYPE_CHECKING:
    from arcrun.types import Tool


@dataclass
class _State:
    """Mutable runtime state shared by the delegate capability."""

    parent_tools: list[Tool]
    parent_sk_bytes: bytes
    config: DelegateConfig


_state: _State | None = None


def configure(
    *,
    parent_tools: list[Tool],
    parent_sk_bytes: bytes | None = None,
    config: DelegateConfig | None = None,
) -> None:
    """Bind delegate module state. Called once at agent startup.

    Args:
        parent_tools: Full tool list available to the parent agent.
            The delegate capability will intersect any child request
            against this list (no privilege escalation).
        parent_sk_bytes: Parent agent's Ed25519 signing key bytes for
            HKDF-based child identity derivation. If None, zero bytes
            are used -- identity derivation still works but produces the
            same key for every spawn (acceptable for development only).
        config: DelegateConfig; defaults to personal-tier config if None.
    """
    global _state
    _state = _State(
        parent_tools=list(parent_tools),
        parent_sk_bytes=parent_sk_bytes or b"\x00" * 32,
        config=config or DelegateConfig(),
    )


def state() -> _State:
    """Return configured runtime state.

    Raises:
        RuntimeError: If :func:`configure` has not been called.
    """
    if _state is None:
        raise RuntimeError(
            "delegate module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Clear runtime state. Test-only helper."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
