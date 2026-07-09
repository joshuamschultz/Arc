"""``task_complete`` builtin tool — SPEC-017 R-030 through R-032.

Structured completion signal for the loop. The agent calls this to
indicate the task is finished (success, partial, failed). The loop
observes the call, terminates cleanly, and emits ``loop.completed``
with the payload.

The tool itself is side-effect-free; loop integration is handled by
the strategy code that consumes the returned payload.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from arcrun.types import Tool, ToolContext

TaskStatus = Literal["success", "partial", "failed"]


class TaskCompleteArgs(BaseModel):
    """Arguments to ``task_complete``.

    ``status`` and ``summary`` are required; everything else is
    optional context. ``error`` is used by the loop itself when
    enforcing budget caps — agents generally won't set it directly.
    """

    model_config = ConfigDict(frozen=True)

    status: TaskStatus
    summary: str
    artifacts: list[str] | None = None
    next_steps: list[str] | None = None
    error: str | None = None


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["success", "partial", "failed"],
            "description": "Outcome of the task attempt.",
        },
        "summary": {
            "type": "string",
            "description": "One-line human-readable summary of what happened.",
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of file paths or URIs produced.",
        },
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of follow-up actions for the user.",
        },
        "error": {
            "type": "string",
            "description": "Optional structured error code (e.g. max_turns, max_cost).",
        },
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}


async def _execute(args: dict[str, Any], _ctx: ToolContext) -> str:
    """Validate the payload and return a summary string.

    The loop watches for this tool's invocation and terminates. The
    returned string is what the assistant sees as the tool result
    (so it can reflect back in its final response if needed).
    """
    try:
        parsed = TaskCompleteArgs.model_validate(args)
    except ValidationError as err:
        # Raise — tool wrapper converts this to a structured error result.
        raise ValueError(f"task_complete invalid payload: {err}") from err
    parts = [f"status={parsed.status}", parsed.summary]
    if parsed.error:
        parts.append(f"error={parsed.error}")
    return " | ".join(parts)


def make_task_complete_tool() -> Tool:
    """Build the ``task_complete`` Tool for registration."""
    return Tool(
        name="task_complete",
        description=(
            "Signal that the task is complete. Call this once with a "
            "status (success|partial|failed) and a one-line summary "
            "when you have finished the user's request."
        ),
        input_schema=_INPUT_SCHEMA,
        execute=_execute,
        timeout_seconds=5.0,
        signals_completion=True,
    )


BudgetBreachReason = Literal["max_turns", "max_cost", "max_tokens", "runaway_loop", "error_cascade"]

_BREACH_SUMMARIES: dict[str, str] = {
    "max_turns": "Turn limit reached before task completed.",
    "max_cost": "Cost limit reached before task completed.",
    "max_tokens": "Token limit reached before task completed.",
    "runaway_loop": "Repeated identical tool call detected — halted as a runaway loop.",
    "error_cascade": "Consecutive tool failures exceeded the cascade threshold — halted.",
}


def make_budget_breach_args(*, reason: BudgetBreachReason) -> TaskCompleteArgs:
    """Synthesize a ``task_complete`` payload when the loop trips the breaker.

    Keeps the breach vocabulary consistent across every site that halts the
    loop — token/cost/turn caps (SPEC-017 R-032, SPEC-038 REQ-003) and the
    SPEC-043 runaway-loop / error-cascade breakers. One terminator factory,
    one reason vocabulary (AC-S2).
    """
    return TaskCompleteArgs(status="failed", summary=_BREACH_SUMMARIES[reason], error=reason)


__all__ = [
    "BudgetBreachReason",
    "TaskCompleteArgs",
    "TaskStatus",
    "make_budget_breach_args",
    "make_task_complete_tool",
]
