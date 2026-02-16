"""Module Bus — async event dispatch with priority, veto, and lifecycle.

Priority ordering: lower values run first (10=policy, 50=security,
100=default, 200=logging). Same-priority handlers run concurrently.
All handlers run even after veto — first veto wins.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from arcagent.core.config import ArcAgentConfig, LLMConfig

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.core.tool_registry import ToolRegistry

_logger = logging.getLogger("arcagent.module_bus")

_DEFAULT_HANDLER_TIMEOUT = 30.0


@dataclass
class EventContext:
    """Context passed to every event handler. Supports veto semantics.

    Data is snapshot-copied on construction to prevent external
    callers from mutating the dict after emit().
    """

    event: str
    data: dict[str, Any]
    agent_did: str
    trace_id: str
    _vetoed: bool = field(default=False, repr=False)
    _veto_reason: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        # Snapshot data to prevent caller mutation after emit()
        object.__setattr__(self, "data", dict(self.data))

    def veto(self, reason: str) -> None:
        """Veto this event. First veto wins. All handlers still run."""
        if not self._vetoed:
            self._vetoed = True
            self._veto_reason = reason

    @property
    def is_vetoed(self) -> bool:
        return self._vetoed

    @property
    def veto_reason(self) -> str:
        return self._veto_reason


@dataclass
class _HandlerRegistration:
    """Internal registration entry for an event handler."""

    event: str
    handler: Callable[[EventContext], Awaitable[None]]
    priority: int = 100
    module_name: str = ""
    timeout_seconds: float = _DEFAULT_HANDLER_TIMEOUT


@dataclass(frozen=True)
class ModuleContext:
    """Dependency injection container for module startup.

    Frozen: modules cannot reassign shared references, but the
    objects themselves are mutable (e.g. modules can call
    tool_registry.register()).
    """

    bus: ModuleBus
    tool_registry: ToolRegistry
    config: ArcAgentConfig
    telemetry: AgentTelemetry
    workspace: Path
    llm_config: LLMConfig


@runtime_checkable
class Module(Protocol):
    """Protocol for modules that register with the bus."""

    @property
    def name(self) -> str: ...

    async def startup(self, ctx: ModuleContext) -> None: ...

    async def shutdown(self) -> None: ...


class ModuleBus:
    """Async event bus with priority dispatch, veto, and module lifecycle."""

    def __init__(self, config: ArcAgentConfig, telemetry: Any) -> None:
        self._config = config
        self._telemetry = telemetry
        self._handlers: dict[str, list[_HandlerRegistration]] = defaultdict(list)
        self._modules: list[Module] = []

    def subscribe(
        self,
        event: str,
        handler: Callable[[EventContext], Awaitable[None]],
        priority: int = 100,
        module_name: str = "",
        timeout_seconds: float = _DEFAULT_HANDLER_TIMEOUT,
    ) -> None:
        """Register handler for event. Lower priority runs first."""
        reg = _HandlerRegistration(
            event=event,
            handler=handler,
            priority=priority,
            module_name=module_name,
            timeout_seconds=timeout_seconds,
        )
        self._handlers[event].append(reg)

    def handler_count(self, event: str) -> int:
        """Number of registered handlers for an event."""
        return len(self._handlers[event])

    def unsubscribe_by_module_prefix(self, prefix: str) -> int:
        """Remove all handlers whose module_name starts with prefix.

        Returns the total number of handlers removed. Used by the
        extension system for hot-reload cleanup.
        """
        removed = 0
        for event in list(self._handlers):
            original = self._handlers[event]
            filtered = [h for h in original if not h.module_name.startswith(prefix)]
            removed += len(original) - len(filtered)
            self._handlers[event] = filtered
        return removed

    async def emit(
        self,
        event: str,
        data: dict[str, Any],
        agent_did: str = "",
        trace_id: str = "",
    ) -> EventContext:
        """Dispatch event to all handlers, grouped by priority.

        Within same priority: concurrent via asyncio.gather.
        Across priorities: sequential (lower first).
        Returns EventContext with veto state.
        """
        ctx = EventContext(
            event=event,
            data=data,
            agent_did=agent_did,
            trace_id=trace_id,
        )

        handlers = self._handlers.get(event, [])
        if not handlers:
            return ctx

        # Group by priority
        by_priority: dict[int, list[_HandlerRegistration]] = defaultdict(list)
        for reg in handlers:
            by_priority[reg.priority].append(reg)

        # Execute groups in priority order (lower first)
        for priority in sorted(by_priority):
            group = by_priority[priority]
            tasks = [self._run_handler(reg, ctx) for reg in group]
            await asyncio.gather(*tasks, return_exceptions=True)

        return ctx

    async def _run_handler(self, reg: _HandlerRegistration, ctx: EventContext) -> None:
        """Run a single handler with timeout and error isolation."""
        try:
            await asyncio.wait_for(
                reg.handler(ctx),
                timeout=reg.timeout_seconds,
            )
        except TimeoutError:
            _logger.warning(
                "Handler %s for event %s timed out after %.1fs",
                reg.module_name or reg.handler.__name__,
                reg.event,
                reg.timeout_seconds,
            )
        except Exception:
            _logger.exception(
                "Handler %s for event %s raised an exception",
                reg.module_name or reg.handler.__name__,
                reg.event,
            )

    def register_module(self, module: Module) -> None:
        """Register a module for lifecycle management."""
        self._modules.append(module)

    async def startup(self, ctx: ModuleContext) -> None:
        """Call module.startup(ctx) for all registered modules in order."""
        for module in self._modules:
            try:
                await module.startup(ctx)
                _logger.info("Module %s started", module.name)
            except Exception:
                _logger.exception("Module %s failed to start", module.name)

    async def shutdown(self) -> None:
        """Call module.shutdown() for all modules in reverse order."""
        for module in reversed(self._modules):
            try:
                await module.shutdown()
                _logger.info("Module %s shut down", module.name)
            except Exception:
                _logger.exception("Module %s failed to shut down", module.name)
