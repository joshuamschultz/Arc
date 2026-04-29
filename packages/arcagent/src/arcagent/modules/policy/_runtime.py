"""Per-agent policy module runtime context.

The policy module's three hooks share state (engine, session messages,
turn count, background tasks). Decorator-stamped functions can't carry
that state in a closure, so it lives in a module-level :class:`_State`
instance configured by the agent at startup.

This mirrors the pattern in :mod:`arcagent.builtins.capabilities._runtime`
and is consistent with the single-agent-per-process model.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.modules.policy.config import PolicyConfig
from arcagent.modules.policy.policy_engine import PolicyEngine

_logger = logging.getLogger("arcagent.modules.policy._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the three policy hooks."""

    config: PolicyConfig
    eval_config: EvalConfig
    workspace: Path
    telemetry: Any
    llm_config: Any
    engine: PolicyEngine
    eval_label: str
    eval_model: Any = None
    session_messages: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    cfg = PolicyConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = workspace.resolve()
    _state = _State(
        config=cfg,
        eval_config=ec,
        workspace=ws,
        telemetry=telemetry,
        llm_config=llm_config,
        engine=PolicyEngine(config=cfg, workspace=ws, telemetry=telemetry),
        eval_label=f"{agent_name}/eval" if agent_name else "eval",
        semaphore=asyncio.Semaphore(ec.max_concurrent),
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "policy module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
