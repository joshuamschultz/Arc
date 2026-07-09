"""SPEC-043 Phase A — checkpoint / resume (arcrun emits, caller persists)."""

from __future__ import annotations

import pytest
from conftest import LLMResponse, Message, MockModel, ToolCall

from arcrun.checkpoint import LoopCheckpoint, apply_checkpoint, to_checkpoint
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import Tool


async def _echo(params: dict, ctx: object) -> str:
    return f"echo:{params.get('input', '')}"


def _tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_echo,
        )
    ]


def _state(bus: EventBus, tools: list[Tool] | None = None) -> RunState:
    reg = ToolRegistry(tools=tools or _tools(), event_bus=bus)
    return RunState(
        messages=[Message(role="user", content="hi")],
        registry=reg,
        event_bus=bus,
        run_id="run-1",
    )


class TestCheckpointRoundTrip:
    def test_to_checkpoint_captures_resumable_state(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus)
        state.turn_count = 3
        state.tokens_used = {"input": 10, "output": 5, "total": 15}
        state.cost_usd = 0.02
        state.strategy_name = "react"
        state.max_turns = 25

        cp = to_checkpoint(state)
        assert cp.turn_count == 3
        assert cp.tokens_used == {"input": 10, "output": 5, "total": 15}
        assert cp.cost_usd == 0.02
        assert cp.tool_names == ["echo"]
        assert cp.strategy_name == "react"
        assert cp.max_turns == 25

    def test_record_round_trip_excludes_messages(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus)
        state.turn_count = 2
        cp = to_checkpoint(state)
        record = cp.to_record()
        assert "messages" not in record  # transcript is persisted separately
        rebuilt = LoopCheckpoint.from_record(record, messages=cp.messages)
        assert rebuilt.turn_count == cp.turn_count
        assert rebuilt.tool_names == cp.tool_names
        assert rebuilt.messages == cp.messages

    def test_apply_checkpoint_restores_state(self) -> None:
        bus = EventBus(run_id="t")
        src = _state(bus)
        src.turn_count = 4
        src.tokens_used = {"input": 3, "output": 2, "total": 5}
        src.cost_usd = 0.5
        cp = to_checkpoint(src)

        bus2 = EventBus(run_id="t2")
        fresh = _state(bus2)
        apply_checkpoint(fresh, cp)
        assert fresh.turn_count == 4
        assert fresh.tokens_used == {"input": 3, "output": 2, "total": 5}
        assert fresh.cost_usd == 0.5


class TestResumeFailClosed:
    def test_apply_checkpoint_refuses_mutated_tool_set(self) -> None:
        bus = EventBus(run_id="t")
        src = _state(bus)
        cp = to_checkpoint(src)

        # Rebuild with a DIFFERENT tool surface — a poisoned resume (ASI06).
        extra = Tool(
            name="danger",
            description="new tool",
            input_schema={"type": "object", "properties": {}},
            execute=_echo,
        )
        bus2 = EventBus(run_id="t2")
        mutated = _state(bus2, tools=[*_tools(), extra])
        with pytest.raises(ValueError, match="tool-name set changed"):
            apply_checkpoint(mutated, cp)


class TestCheckpointEmission:
    @pytest.mark.asyncio
    async def test_one_checkpoint_per_turn_boundary(self) -> None:
        bus = EventBus(run_id="t")
        captured: list[LoopCheckpoint] = []
        state = _state(bus)
        state.on_checkpoint = captured.append
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        await react_loop(model, state, sandbox, max_turns=5)
        # Two turn boundaries → two checkpoints; last carries turn_count == 2.
        assert len(captured) == 2
        assert captured[-1].turn_count == 2

    @pytest.mark.asyncio
    async def test_no_hook_zero_overhead(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus)
        assert state.on_checkpoint is None
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)
        # No hook → no crash, loop runs normally.
        result = await react_loop(model, state, sandbox, max_turns=5)
        assert result.content == "done"


class TestResumeReentry:
    @pytest.mark.asyncio
    async def test_resume_reenters_at_saved_turn(self) -> None:
        """A run seeded from a checkpoint re-enters at cp.turn_count."""
        bus = EventBus(run_id="t")
        state = _state(bus)
        # Simulate two completed turns captured in a checkpoint.
        state.turn_count = 2
        cp = to_checkpoint(state)

        bus2 = EventBus(run_id="t2")
        resumed = _state(bus2)
        apply_checkpoint(resumed, cp)
        # Only one more response needed — completed turns are NOT re-executed.
        model = MockModel([LLMResponse(content="finished", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus2)
        result = await react_loop(model, resumed, sandbox, max_turns=5)
        assert result.content == "finished"
        # Re-entered at turn 2, ran one more → 3 total.
        assert result.turns == 3
