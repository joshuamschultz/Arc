"""Internal mutable state for a single run() execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
    steer_queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    followup_queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    # Caller hook applied to the message list before each model call.
    # CONTRACT: append-only between turns — the returned list must keep the
    # input's prefix so the provider cache prefix stays valid; only the tail
    # may grow. A deliberate compaction may return a *shorter* list (a one-time
    # boundary reset), but must never rewrite/reorder earlier messages per turn.
    # Set ARCRUN_ASSERT_APPEND_ONLY=1 to enforce this in dev.
    transform_context: Callable[..., Any] | None = None
    tool_timeout: float | None = None
    strategy_name: str = ""
    tool_choice: dict[str, Any] | None = None
    # SPEC-017 R-030/R-032 — structured task completion.
    # When ``task_complete`` fires, the tool handler stores its payload
    # here so the strategy can terminate cleanly on the next check.
    # ``None`` means no termination requested.
    completion_payload: dict[str, Any] | None = None
    # Name of the tool whose ``signals_completion=True`` flag ended the
    # loop. Surfaces on LoopResult so callers can distinguish multiple
    # terminator tools without re-scanning the event chain.
    completion_tool: str | None = None
    # Hard caps from config; enforced at the top of each turn.
    max_cost_usd: float | None = None
