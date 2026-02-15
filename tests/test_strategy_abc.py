"""Tests for Strategy ABC and ReactStrategy wrapper."""
import pytest

from conftest import LLMResponse, MockModel

from arcrun.types import Tool


async def _echo(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


def _tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo input",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_echo,
        )
    ]


class TestStrategyABC:
    def test_strategy_cannot_be_instantiated(self):
        from arcrun.strategies import Strategy

        with pytest.raises(TypeError):
            Strategy()

    def test_strategy_has_abstract_name(self):
        from arcrun.strategies import Strategy

        assert hasattr(Strategy, "name")

    def test_strategy_has_abstract_description(self):
        from arcrun.strategies import Strategy

        assert hasattr(Strategy, "description")

    def test_strategy_has_abstract_call(self):
        from arcrun.strategies import Strategy

        assert hasattr(Strategy, "__call__")


class TestReactStrategy:
    def test_react_strategy_has_name(self):
        from arcrun.strategies.react import ReactStrategy

        s = ReactStrategy()
        assert s.name == "react"

    def test_react_strategy_has_description(self):
        from arcrun.strategies.react import ReactStrategy

        s = ReactStrategy()
        assert isinstance(s.description, str)
        assert len(s.description) > 0

    @pytest.mark.asyncio
    async def test_react_strategy_produces_loop_result(self):
        from arcrun.strategies.react import ReactStrategy

        from arcrun._messages import system_message, user_message
        from arcrun.events import EventBus
        from arcrun.registry import ToolRegistry
        from arcrun.sandbox import Sandbox
        from arcrun.state import RunState
        from arcrun.types import LoopResult

        model = MockModel([LLMResponse(content="Hello!", stop_reason="end_turn")])
        bus = EventBus(run_id="test")
        state = RunState(
            messages=[system_message("Be helpful."), user_message("Say hello")],
            registry=ToolRegistry(tools=_tools(), event_bus=bus),
            event_bus=bus,
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        strategy = ReactStrategy()
        result = await strategy(model, state, sandbox, max_turns=5)

        assert isinstance(result, LoopResult)
        assert result.content == "Hello!"

    def test_react_strategy_is_strategy_subclass(self):
        from arcrun.strategies import Strategy
        from arcrun.strategies.react import ReactStrategy

        assert issubclass(ReactStrategy, Strategy)
