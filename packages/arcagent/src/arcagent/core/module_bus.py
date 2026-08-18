"""Module Bus — async event dispatch with priority and veto.

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
from typing import Any

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
    token: int = 0


@dataclass(frozen=True)
class SubscriptionToken:
    """Opaque handle used to remove one exact bus subscription."""

    event: str
    value: int


class ModuleBus:
    """Async event bus with priority dispatch and veto."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[_HandlerRegistration]] = defaultdict(list)
        self._next_token = 1

    def subscribe(
        self,
        event: str,
        handler: Callable[[EventContext], Awaitable[None]],
        priority: int = 100,
        module_name: str = "",
        timeout_seconds: float = _DEFAULT_HANDLER_TIMEOUT,
    ) -> SubscriptionToken:
        """Register handler for event. Lower priority runs first."""
        token = SubscriptionToken(event=event, value=self._next_token)
        self._next_token += 1
        reg = _HandlerRegistration(
            event=event,
            handler=handler,
            priority=priority,
            module_name=module_name,
            timeout_seconds=timeout_seconds,
            token=token.value,
        )
        self._handlers[event].append(reg)
        return token

    def unsubscribe(self, token: SubscriptionToken) -> bool:
        """Remove the exact subscription represented by ``token``."""
        handlers = self._handlers.get(token.event)
        if not handlers:
            return False
        kept = [registration for registration in handlers if registration.token != token.value]
        if len(kept) == len(handlers):
            return False
        if kept:
            self._handlers[token.event] = kept
        else:
            self._handlers.pop(token.event, None)
        return True

    def replace_handlers(
        self,
        *,
        module_prefix: str,
        handlers: list[tuple[str, Callable[[EventContext], Awaitable[None]], int, str]],
    ) -> tuple[SubscriptionToken, ...]:
        """Atomically replace every handler owned by a module-name prefix.

        The operation contains no await point: emitters see either the old set
        or the complete new set, never a partially rebuilt capability bridge.
        """
        replacements: list[_HandlerRegistration] = []
        tokens: list[SubscriptionToken] = []
        for event, handler, priority, module_name in handlers:
            token = SubscriptionToken(event=event, value=self._next_token)
            self._next_token += 1
            tokens.append(token)
            replacements.append(
                _HandlerRegistration(
                    event=event,
                    handler=handler,
                    priority=priority,
                    module_name=module_name,
                    token=token.value,
                )
            )

        for event in tuple(self._handlers):
            kept = [
                registration
                for registration in self._handlers[event]
                if not registration.module_name.startswith(module_prefix)
            ]
            if kept:
                self._handlers[event] = kept
            else:
                self._handlers.pop(event, None)
        for registration in replacements:
            self._handlers[registration.event].append(registration)
        return tuple(tokens)

    def handler_count(self, event: str) -> int:
        """Number of registered handlers for an event."""
        return len(self._handlers[event])

    def handler_count_by_module(self, event: str, module_name: str) -> int:
        """Number of handlers for ``event`` registered under ``module_name``.

        Used by the capability bridge to detect already-subscribed hooks
        across reloads so we never double-subscribe.
        """
        return sum(1 for h in self._handlers.get(event, ()) if h.module_name == module_name)

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
            # _run_handler is the single exception-isolation boundary.
            await asyncio.gather(*tasks)

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
        except Exception:  # reason: fail-open — log + continue
            _logger.exception(
                "Handler %s for event %s raised an exception",
                reg.module_name or reg.handler.__name__,
                reg.event,
            )
