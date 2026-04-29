"""Per-agent user_profile module runtime context.

The two hooks and the three tools share state (config, workspace, store,
telemetry). Decorator-stamped functions can't carry that state in a closure,
so it lives in a module-level :class:`_State` instance configured by the
agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.modules.memory._runtime`. Single-agent-per-process is the
assumption; this is shared mutable state for one agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.store import ProfileStore

_logger = logging.getLogger("arcagent.modules.user_profile._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across user_profile hooks and tools."""

    config: UserProfileConfig
    workspace: Path
    telemetry: Any
    store: ProfileStore
    agent_name: str


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    agent_name: str = "",
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = UserProfileConfig(**(config or {}))
    ws = workspace.resolve()
    _state = _State(
        config=cfg,
        workspace=ws,
        telemetry=telemetry,
        store=ProfileStore(ws, cfg, telemetry=telemetry),
        agent_name=agent_name,
    )
    _logger.info(
        "user_profile module runtime configured workspace=%s profile_dir=%s",
        ws,
        cfg.profile_dir,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "user_profile module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
