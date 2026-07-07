"""SPEC-043 Phase B — proactive HITL pause (arcrun pauses, provider decides)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import LLMResponse, Message, MockModel, ToolCall

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import Tool


async def _echo(params: dict, ctx: object) -> str:
    return f"echo:{params.get('input', '')}"


def _state(bus: EventBus, **kw: object) -> RunState:
    reg = ToolRegistry(
        tools=[
            Tool(
                name="echo",
                description="Echo",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                execute=_echo,
            )
        ],
        event_bus=bus,
    )
    state = RunState(
        messages=[Message(role="user", content="go")],
        registry=reg,
        event_bus=bus,
        run_id="run",
    )
    for k, v in kw.items():
        setattr(state, k, v)
    return state


def _one_call_then_done() -> MockModel:
    return MockModel(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"input": "hi"})],
                stop_reason="tool_use",
            ),
            LLMResponse(content="done", stop_reason="end_turn"),
        ]
    )


class TestApprovalPause:
    @pytest.mark.asyncio
    async def test_grant_dispatches_the_call(self) -> None:
        bus = EventBus(run_id="t")
        seen: list[Any] = []

        async def provider(tc: Any) -> object:
            seen.append(tc.name)
            return object()  # opaque grant → proceed

        state = _state(
            bus, approval_provider=provider, approval_required_tools=frozenset({"echo"})
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        result = await react_loop(_one_call_then_done(), state, sandbox, max_turns=5)
        assert seen == ["echo"]  # loop awaited the provider before dispatch
        assert result.content == "done"
        # The tool actually ran (echo result recorded, not the denial message).
        tool_msgs = [m for m in state.messages if getattr(m, "role", "") == "tool"]
        assert any("echo:hi" in m.content[0].content for m in tool_msgs)

    @pytest.mark.asyncio
    async def test_none_fails_closed_no_dispatch(self) -> None:
        bus = EventBus(run_id="t")
        executed: list[str] = []

        async def _watch_echo(params: dict, ctx: object) -> str:
            executed.append("ran")
            return "should-not-run"

        reg = ToolRegistry(
            tools=[
                Tool(
                    name="echo",
                    description="Echo",
                    input_schema={
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                    },
                    execute=_watch_echo,
                )
            ],
            event_bus=bus,
        )

        async def provider(tc: Any) -> None:
            return None  # deny / timeout / no channel

        state = RunState(
            messages=[Message(role="user", content="go")],
            registry=reg,
            event_bus=bus,
            run_id="run",
            approval_provider=provider,
            approval_required_tools=frozenset({"echo"}),
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        await react_loop(_one_call_then_done(), state, sandbox, max_turns=5)
        assert executed == []  # fail closed — the tool never ran
        tool_msgs = [m for m in state.messages if getattr(m, "role", "") == "tool"]
        assert any("approval required" in m.content[0].content for m in tool_msgs)
        # Audit trail: required + denied events emitted (REQ-061).
        types = {e.type for e in bus.events}
        assert "approval.required" in types
        assert "approval.denied" in types

    @pytest.mark.asyncio
    async def test_unflagged_tool_skips_provider(self) -> None:
        bus = EventBus(run_id="t")
        called = False

        async def provider(tc: Any) -> object:
            nonlocal called
            called = True
            return object()

        # No tools flagged → provider never consulted.
        state = _state(
            bus, approval_provider=provider, approval_required_tools=frozenset()
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        result = await react_loop(_one_call_then_done(), state, sandbox, max_turns=5)
        assert called is False
        assert result.content == "done"
