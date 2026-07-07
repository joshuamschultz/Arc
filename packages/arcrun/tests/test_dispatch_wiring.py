"""SPEC-043 Phase D — parallel_dispatch is the ONE dispatch path (AC-S1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import LLMResponse, Message, MockModel, ToolCall

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import Tool

_REACT_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "arcrun" / "strategies" / "react.py"
).read_text(encoding="utf-8")


class TestDeadPathDeleted:
    def test_react_imports_parallel_dispatch(self) -> None:
        assert "from arcrun.parallel_dispatch import" in _REACT_SRC
        assert "dispatch_batch" in _REACT_SRC

    def test_react_has_no_adhoc_gather(self) -> None:
        # The ad-hoc asyncio.gather path was deleted (REQ-030 / AC-S1).
        assert "asyncio.gather" not in _REACT_SRC
        assert "parallel_queue" not in _REACT_SRC

    def test_parallel_dispatch_has_production_importer(self) -> None:
        """parallel_dispatch.py now has a non-test importer (react.py)."""
        assert "import" in _REACT_SRC and "parallel_dispatch" in _REACT_SRC


async def _slow(params: dict, ctx: object) -> str:
    return f"ok:{params.get('n')}"


async def _fail(params: dict, ctx: object) -> str:
    raise ValueError("nope")


class TestGatedDispatch:
    @pytest.mark.asyncio
    async def test_submission_order_preserved_and_failure_isolated(self) -> None:
        bus = EventBus(run_id="t")
        reg = ToolRegistry(
            tools=[
                Tool(
                    name="ro",
                    description="read only",
                    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                    execute=_slow,
                    classification="read_only",
                ),
                Tool(
                    name="bad",
                    description="fails",
                    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                    execute=_fail,
                    classification="read_only",
                ),
            ],
            event_bus=bus,
        )
        state = RunState(
            messages=[Message(role="user", content="go")],
            registry=reg,
            event_bus=bus,
            run_id="run",
        )
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="c0", name="ro", arguments={"n": 0}),
                        ToolCall(id="c1", name="bad", arguments={"n": 1}),
                        ToolCall(id="c2", name="ro", arguments={"n": 2}),
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        await react_loop(model, state, sandbox, max_turns=5)
        tool_msgs = [m for m in state.messages if getattr(m, "role", "") == "tool"]
        # Submission order: c0, c1, c2 — a partial failure did not abort siblings.
        assert "ok:0" in tool_msgs[0].content[0].content
        assert "Error" in tool_msgs[1].content[0].content
        assert "ok:2" in tool_msgs[2].content[0].content
