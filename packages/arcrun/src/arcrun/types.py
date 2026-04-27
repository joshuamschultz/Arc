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
    """A tool the model can call.

    parallel_safe:
        When True, the loop may dispatch multiple calls to this tool in
        the same turn concurrently via asyncio.gather. The tool MUST be
        safe to run alongside itself — independent state per call,
        no shared mutable resource. Default False (serial dispatch).

    signals_completion:
        When True, an invocation of this tool terminates the loop. The
        tool's call arguments become the completion payload (status,
        summary, etc.). Used for structured terminators like
        ``task_complete`` without the loop needing to know the tool's
        name. Default False.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Callable[[dict[str, Any], ToolContext], Awaitable[str]]
    timeout_seconds: float | None = None
    parallel_safe: bool = False
    signals_completion: bool = False


@dataclass
class ToolContext:
    """Passed to Tool.execute.

    Attributes:
        parent_state: Live RunState from the parent execution. Set by the
            executor when the tool is called so tools (e.g., delegate) can
            read depth, max_depth, and token_budget without importing RunState
            directly. None for tools called outside a running loop (tests,
            standalone invocations).
    """

    run_id: str
    tool_call_id: str
    turn_number: int
    event_bus: EventBus | None
    cancelled: asyncio.Event
    parent_state: Any = None


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
