"""Tests for model-based strategy selection."""

import pytest
from conftest import LLMResponse, MockModel, ToolCall

from arcrun._messages import system_message, user_message
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.strategies import select_strategy
from arcrun.types import Tool


async def _echo(params: dict, ctx: object) -> str:
    return "echo"


def _make_state(bus: EventBus) -> RunState:
    tools = [
        Tool(
            name="echo",
            description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_echo,
        )
    ]
    return RunState(
        messages=[system_message("Be helpful."), user_message("Do something.")],
        registry=ToolRegistry(tools=tools, event_bus=bus),
        event_bus=bus,
    )


class TestStrategySelection:
    @pytest.mark.asyncio
    async def test_none_returns_react(self):
        model = MockModel([])
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        result = await select_strategy(None, model, state)
        assert result == "react"
        assert model._call_count == 0

    @pytest.mark.asyncio
    async def test_single_returns_direct_no_call(self):
        model = MockModel([])
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        result = await select_strategy(["code"], model, state)
        assert result == "code"
        assert model._call_count == 0

    @pytest.mark.asyncio
    async def test_multiple_model_picks(self):
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "code", "reasoning": "needs computation"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            ]
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        result = await select_strategy(["react", "code"], model, state)
        assert result == "code"

    @pytest.mark.asyncio
    async def test_invalid_model_output_fallback_react(self):
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "nonexistent"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            ]
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        result = await select_strategy(["react", "code"], model, state)
        assert result == "react"

    @pytest.mark.asyncio
    async def test_model_error_fallback_react(self):
        class FailModel:
            async def invoke(self, messages, tools=None):
                raise ConnectionError("API down")

        bus = EventBus(run_id="test")
        state = _make_state(bus)

        result = await select_strategy(["react", "code"], FailModel(), state)
        assert result == "react"

    @pytest.mark.asyncio
    async def test_selection_start_event(self):
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "react"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            ]
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        await select_strategy(["react", "code"], model, state)

        start_events = [e for e in bus.events if e.type == "strategy.selection.start"]
        assert len(start_events) == 1
        assert start_events[0].data["allowed_strategies"] == ["react", "code"]

    @pytest.mark.asyncio
    async def test_selection_complete_event(self):
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "code", "reasoning": "best fit"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            ]
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        await select_strategy(["react", "code"], model, state)

        complete_events = [e for e in bus.events if e.type == "strategy.selection.complete"]
        assert len(complete_events) == 1
        assert complete_events[0].data["selected"] == "code"
        assert complete_events[0].data["reasoning"] == "best fit"

    @pytest.mark.asyncio
    async def test_fallback_event_on_invalid(self):
        model = MockModel([LLMResponse(content="I choose react", stop_reason="end_turn")])
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        await select_strategy(["react", "code"], model, state)

        fallback_events = [e for e in bus.events if e.type == "strategy.selection.fallback"]
        assert len(fallback_events) == 1

    @pytest.mark.asyncio
    async def test_model_sees_strategy_descriptions(self):
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "react"},
                        )
                    ],
                    stop_reason="tool_use",
                )
            ]
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        await select_strategy(["react", "code"], model, state)

        call = model.invoke_calls[0]
        system_content = call["messages"][0].content
        assert "react" in system_content
        assert "code" in system_content

    @pytest.mark.asyncio
    async def test_no_events_for_single_strategy(self):
        model = MockModel([])
        bus = EventBus(run_id="test")
        state = _make_state(bus)

        await select_strategy(["react"], model, state)

        selection_events = [e for e in bus.events if "selection" in e.type]
        assert len(selection_events) == 0
