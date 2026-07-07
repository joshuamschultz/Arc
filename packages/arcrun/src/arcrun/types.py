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

    classification:
        Dispatch classification consumed by ``parallel_dispatch``'s
        ``BatchClassifier`` to decide whether a turn's calls may run
        concurrently. ``"read_only"`` tools with no shared resource may
        parallelize; anything else forces sequential dispatch. Default
        ``"state_modifying"`` — fail-closed so an unclassified tool never
        runs concurrently by accident (SPEC-043 REQ-034). The owning
        deployment (arcagent) sets the real value when it builds the tool.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    execute: Callable[[dict[str, Any], ToolContext], Awaitable[str]]
    timeout_seconds: float | None = None
    parallel_safe: bool = False
    signals_completion: bool = False
    classification: str = "state_modifying"


@dataclass
class ToolContext:
    """Passed to Tool.execute.

    Attributes:
        parent_state: Live RunState from the parent execution. Set by the
            executor when the tool is called so tools (e.g., delegate) can
            read depth, max_depth, and budget usage without importing RunState
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
    """Returned by run().

    ``completion_payload`` and ``completion_tool`` surface the structured
    terminator output (SPEC-017 R-030). When a tool flagged
    ``signals_completion=True`` ends the loop, its (validated) arguments
    are exposed here so callers reading structured stage outputs don't
    have to capture them via a closure on the tool's ``execute`` callable
    or fish through ``events`` for the ``loop.completed`` event payload.
    Both are ``None`` when the loop terminated by ``stop_reason==end_turn``
    or hit ``max_turns``/``max_cost``.
    """

    content: str | None
    turns: int
    tool_calls_made: int
    tokens_used: dict[str, Any]
    strategy_used: str
    cost_usd: float
    events: list[Event] = field(default_factory=list)
    completion_payload: dict[str, Any] | None = None
    completion_tool: str | None = None

    def verify_integrity(self) -> ChainVerificationResult:
        """Verify tamper-evidence of the event chain."""
        from arcrun.events import verify_chain

        return verify_chain(self.events)
