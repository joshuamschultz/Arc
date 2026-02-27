"""Public type contracts for arcrun."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcrun.events import ChainVerificationResult, Event, EventBus


@dataclass
class Tool:
    """A tool the model can call."""

    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Callable[[dict[str, Any], ToolContext], Awaitable[str]]
    timeout_seconds: float | None = None


@dataclass
class ToolContext:
    """Passed to Tool.execute."""

    run_id: str
    tool_call_id: str
    turn_number: int
    event_bus: EventBus | None
    cancelled: asyncio.Event


@dataclass
class SandboxConfig:
    """Permission boundary. allowed_tools=None means all allowed."""

    allowed_tools: list[str] | None = None
    check: Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]] | None = None


@dataclass
class LoopResult:
    """Returned by run()."""

    content: str | None
    turns: int
    tool_calls_made: int
    tokens_used: dict[str, Any]
    strategy_used: str
    cost_usd: float
    events: list[Event] = field(default_factory=list)

    def verify_integrity(self) -> ChainVerificationResult:
        """Verify tamper-evidence of the event chain."""
        from arcrun.events import verify_chain

        return verify_chain(self.events)
