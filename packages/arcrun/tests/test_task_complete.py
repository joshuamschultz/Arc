"""SPEC-017 Phase 5 — ``task_complete`` builtin and loop termination.

The agent signals task completion by calling ``task_complete`` with a
structured status/summary payload. The loop observes the call,
terminates cleanly, and emits a ``loop.completed`` event carrying the
payload.

Also verifies budget caps: ``max_turns`` and ``max_cost_usd`` breach
→ automatic ``task_complete(status="failed", error=...)`` call.
"""

from __future__ import annotations

from typing import Any

import pytest


class TestTaskCompleteArgs:
    """Structured schema: status required, summary required, rest optional."""

    def test_minimal_args(self) -> None:
        from arcrun.builtins.task_complete import TaskCompleteArgs

        args = TaskCompleteArgs(status="success", summary="done")
        assert args.status == "success"
        assert args.summary == "done"
        assert args.artifacts is None
        assert args.next_steps is None
        assert args.error is None

    def test_rejects_invalid_status(self) -> None:
        from pydantic import ValidationError

        from arcrun.builtins.task_complete import TaskCompleteArgs

        with pytest.raises(ValidationError):
            TaskCompleteArgs(status="made_up", summary="x")  # type: ignore[arg-type]

    def test_failed_status_with_error_message(self) -> None:
        from arcrun.builtins.task_complete import TaskCompleteArgs

        args = TaskCompleteArgs(
            status="failed",
            summary="ran out of turns",
            error="max_turns",
        )
        assert args.error == "max_turns"

    def test_full_payload(self) -> None:
        from arcrun.builtins.task_complete import TaskCompleteArgs

        args = TaskCompleteArgs(
            status="partial",
            summary="completed 2 of 3 steps",
            artifacts=["/tmp/out.txt"],
            next_steps=["finish step 3"],
        )
        assert args.artifacts == ["/tmp/out.txt"]
        assert args.next_steps == ["finish step 3"]


class TestTaskCompleteTool:
    """``task_complete`` is a registered tool — callable, returns a summary."""

    async def test_tool_returns_status_summary(self) -> None:
        from arcrun.builtins.task_complete import make_task_complete_tool

        tool = make_task_complete_tool()
        assert tool.name == "task_complete"
        result = await tool.execute(
            {"status": "success", "summary": "all good"},
            _CtxStub(),
        )
        assert "success" in result
        assert "all good" in result

    async def test_tool_rejects_missing_status(self) -> None:
        from arcrun.builtins.task_complete import make_task_complete_tool

        tool = make_task_complete_tool()
        with pytest.raises(Exception):  # noqa: B017 — any validation error
            await tool.execute({"summary": "no status"}, _CtxStub())


class TestLimitEnforcement:
    """Budget caps enforce via task_complete(failed, error=...) synthetic call."""

    def test_max_turns_error_code(self) -> None:
        from arcrun.builtins.task_complete import make_budget_breach_args

        args = make_budget_breach_args(reason="max_turns")
        assert args.status == "failed"
        assert args.error == "max_turns"
        assert "turn" in args.summary.lower()

    def test_max_cost_error_code(self) -> None:
        from arcrun.builtins.task_complete import make_budget_breach_args

        args = make_budget_breach_args(reason="max_cost")
        assert args.status == "failed"
        assert args.error == "max_cost"


class _CtxStub:
    """Minimal stand-in for ToolContext."""

    run_id = "r"
    tool_call_id = "tc"
    turn_number = 1
    event_bus = None
    cancelled: Any = None
