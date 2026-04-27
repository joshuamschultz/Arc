"""Scheduler module — agent self-scheduling with cron, interval, and one-time tasks."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.core.module_bus import ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.scheduler.config import SchedulerConfig

if TYPE_CHECKING:
    from arcagent.modules.scheduler.scheduler import SchedulerEngine

_logger = logging.getLogger("arcagent.scheduler")


class SchedulerModule:
    """Scheduling module — Module Bus participant.

    Provides 4 CRUD tools for the LLM and a background timer loop
    that evaluates and fires schedules.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        # Lazy import: ScheduleStore → models → croniter (optional dependency)
        from arcagent.modules.scheduler.store import ScheduleStore as _Store

        self._config = SchedulerConfig(**(config or {}))
        self._telemetry = telemetry
        self._workspace = workspace
        self._store = _Store(workspace / self._config.store_path)
        self._engine: SchedulerEngine | None = None

    @property
    def name(self) -> str:
        return "scheduler"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register tools, subscribe to events, start engine."""
        # Lazy imports: croniter is an optional dependency required only at runtime
        from arcagent.modules.scheduler.scheduler import SchedulerEngine as _Engine
        from arcagent.modules.scheduler.tools import create_scheduler_tools

        # Create and register tools.
        if self._telemetry is None:
            raise RuntimeError(
                "SchedulerModule requires AgentTelemetry; "
                "pass telemetry= when constructing the module."
            )
        tools = create_scheduler_tools(
            store=self._store,
            config=self._config,
            telemetry=self._telemetry,
        )
        for tool in tools:
            ctx.tool_registry.register(tool)

        # Subscribe to agent:shutdown for graceful cleanup.
        ctx.bus.subscribe("agent:shutdown", self._on_agent_shutdown)

        # Subscribe to agent:ready for deferred binding of agent.run().
        ctx.bus.subscribe("agent:ready", self._on_agent_ready)

        # Create and start the engine.
        # agent_run_fn will be bound later via set_agent_run_fn or ctx.
        agent_run_fn = getattr(ctx, "agent_run_fn", None)
        if agent_run_fn is None:

            async def _noop(prompt: str, **kwargs: Any) -> str:
                _logger.warning("No agent_run_fn bound; schedule '%s' skipped", prompt)
                return ""

            agent_run_fn = _noop

        has_real_run_fn = getattr(ctx, "agent_run_fn", None) is not None

        self._engine = _Engine(
            store=self._store,
            config=self._config,
            telemetry=self._telemetry,
            agent_run_fn=agent_run_fn,
            bus=ctx.bus,
        )

        # If a real agent_run_fn was provided at startup (e.g. tests),
        # signal readiness immediately so the timer loop doesn't block.
        if has_real_run_fn:
            self._engine.set_agent_run_fn(agent_run_fn)

        await self._engine.start()
        _logger.info("Scheduler module started")

    async def shutdown(self) -> None:
        """Stop engine and persist final state. Safe to call multiple times."""
        if self._engine is not None:
            await self._engine.stop()
            self._engine = None
        _logger.info("Scheduler module stopped")

    def set_agent_run_fn(self, fn: Callable[..., Awaitable[Any]]) -> None:
        """Bind the agent.run() callback after startup (deferred binding)."""
        if self._engine is not None:
            self._engine.set_agent_run_fn(fn)

    async def _on_agent_ready(self, event: Any) -> None:
        """Handle agent:ready — bind agent.run() callback."""
        data = event.data if hasattr(event, "data") else {}
        run_fn = data.get("run_fn")
        if run_fn is not None:
            self.set_agent_run_fn(run_fn)
            _logger.info("Bound agent_run_fn via agent:ready event")

    async def _on_agent_shutdown(self, event: Any) -> None:
        """Handle agent:shutdown event."""
        await self.shutdown()


__all__ = ["SchedulerModule"]
