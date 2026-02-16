"""Internal mutable state for a single run() execution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry


@dataclass
class RunState:
    """Internal state during execution. Not part of public API."""

    messages: list[Any]
    registry: ToolRegistry
    event_bus: EventBus
    turn_count: int = 0
    tokens_used: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "total": 0}
    )
    cost_usd: float = 0.0
    tool_calls_made: int = 0
    run_id: str = ""
    depth: int = 0
    max_depth: int = 3
    parent_run_id: str = ""
    token_budget: int | None = None
    cost_budget: float | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    steer_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    followup_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    transform_context: Callable[..., Any] | None = None
    tool_timeout: float | None = None
    strategy_name: str = ""
