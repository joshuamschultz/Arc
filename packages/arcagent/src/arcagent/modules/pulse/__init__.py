"""Pulse module — periodic ambient awareness for agents.

Reads pulse.md for a check list, executes all overdue checks
via agent_run_fn. Both humans and agents can edit pulse.md.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from arcagent.core.module_bus import ModuleContext

if TYPE_CHECKING:
    from arcagent.modules.pulse.engine import PulseEngine

_logger = logging.getLogger("arcagent.pulse")


# --- Config & Models ---


class PulseConfig(BaseModel):
    """Configuration for the pulse module."""

    enabled: bool = True
    interval_seconds: int = Field(default=600, ge=10)
    pulse_file: str = "pulse.md"
    state_file: str = "pulse-state.json"
    timeout_seconds: float = Field(default=300.0, gt=0)


class PulseCheck(BaseModel):
    """A single check defined in pulse.md."""

    name: str
    interval_minutes: int
    action: str


class PulseCheckState(BaseModel):
    """Runtime state for a single check."""

    last_run: str | None = None
    last_result: str | None = None
    consecutive_failures: int = 0


class PulseState(BaseModel):
    """Full pulse state persisted to pulse-state.json."""

    checks: dict[str, PulseCheckState] = {}


# --- Module ---


class PulseModule:
    """Pulse module — Module Bus participant."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
        **_kw: Any,
    ) -> None:
        self._config = PulseConfig(**(config or {}))
        self._workspace = workspace
        self._engine: PulseEngine | None = None

    @property
    def name(self) -> str:
        return "pulse"

    async def startup(self, ctx: ModuleContext) -> None:
        """Subscribe to events and start pulse engine."""
        from arcagent.modules.pulse.engine import PulseEngine as _Engine

        ctx.bus.subscribe("agent:shutdown", self._on_shutdown)
        ctx.bus.subscribe("agent:ready", self._on_ready)

        agent_run_fn = getattr(ctx, "agent_run_fn", None)
        if agent_run_fn is None:

            async def _noop(prompt: str, **kwargs: Any) -> str:
                _logger.warning("No agent_run_fn bound; pulse check skipped")
                return ""

            agent_run_fn = _noop

        has_real_run_fn = getattr(ctx, "agent_run_fn", None) is not None

        self._engine = _Engine(
            workspace=self._workspace,
            config=self._config,
            agent_run_fn=agent_run_fn,
            bus=ctx.bus,
        )

        if has_real_run_fn:
            self._engine.set_agent_run_fn(agent_run_fn)

        await self._engine.start()
        _logger.info("Pulse module started (interval=%ds)", self._config.interval_seconds)

    async def shutdown(self) -> None:
        """Stop engine. Safe to call multiple times."""
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None

    def set_agent_run_fn(self, fn: Callable[..., Awaitable[Any]]) -> None:
        """Bind the agent.run() callback after startup."""
        if self._engine is not None:
            self._engine.set_agent_run_fn(fn)

    async def _on_ready(self, event: Any) -> None:
        data = event.data if hasattr(event, "data") else {}
        run_fn = data.get("run_fn")
        if run_fn is not None:
            self.set_agent_run_fn(run_fn)
            _logger.info("Bound agent_run_fn via agent:ready event")

    async def _on_shutdown(self, event: Any) -> None:
        await self.shutdown()


__all__ = ["PulseModule"]
