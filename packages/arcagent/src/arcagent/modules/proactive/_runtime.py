"""Per-agent proactive module runtime context.

The decorator-form proactive module (``capabilities.py``) cannot carry
state in a closure — ``@hook`` and ``@capability`` stamps wrap plain
functions/classes and the capability class is instantiated by the
loader with no arguments. Runtime state (engine, leader, config,
telemetry) therefore lives on a module-level :class:`_State` instance
configured by the agent at startup.

This mirrors :mod:`arcagent.modules.scheduler._runtime` and is
consistent with the single-agent-per-process model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.modules.proactive.engine import ProactiveEngine
    from arcagent.modules.proactive.leader import LeaderElection

_logger = logging.getLogger("arcagent.modules.proactive._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the proactive capability + hooks."""

    config: dict[str, Any]
    workspace: Path
    telemetry: AgentTelemetry | None
    agent_name: str
    llm_config: Any
    leader: LeaderElection
    engine: ProactiveEngine | None = None
    # asyncio task handle for the running tick loop; stored so teardown
    # can cancel it without the capability needing to track it externally.
    _tick_task: Any = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: AgentTelemetry | None = None,
    workspace: Path = Path("."),
    agent_name: str = "",
    llm_config: Any = None,
) -> None:
    """Bind module state. Called once at agent startup.

    ``leader`` defaults to :class:`~arcagent.modules.proactive.leader.NoOpLeaderElection`
    (single-instance / personal tier). Enterprise / federal deployments
    pass a Redis or Kubernetes lease-backed implementation via config.
    """
    global _state
    from arcagent.modules.proactive.leader import NoOpLeaderElection

    _state = _State(
        config=config or {},
        workspace=workspace.resolve(),
        telemetry=telemetry,
        agent_name=agent_name,
        llm_config=llm_config,
        leader=NoOpLeaderElection(),
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "proactive module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
