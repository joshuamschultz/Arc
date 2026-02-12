"""Public type contracts for arcrun. No business logic."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from arcrun.events import EventBus


@dataclass
class Tool:
    """A tool the model can call. Use factory functions for complex tools."""

    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Callable[[dict[str, Any], ToolContext], Awaitable[str]]


@dataclass
class ToolContext:
    """Passed to Tool.execute. Provides environment awareness and cancel signal."""

    run_id: str
    tool_call_id: str
    turn_number: int
    event_bus: EventBus | None
    cancelled: asyncio.Event


@dataclass
class SandboxConfig:
    """Permission boundary. allowed_tools=None means no sandbox (all allowed)."""

    allowed_tools: list[str] | None = None
    check: Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]] | None = None


@dataclass
class LoopResult:
    """Returned by run(). Complete execution summary."""

    content: str | None
    turns: int
    tool_calls_made: int
    tokens_used: dict[str, Any]
    strategy_used: str
    cost_usd: float
    events: list[Any] = field(default_factory=list)
