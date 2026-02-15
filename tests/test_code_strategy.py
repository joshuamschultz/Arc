"""Tests for CodeExecStrategy."""
import pytest

from conftest import LLMResponse, MockModel

from arcrun._messages import system_message, user_message
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import LoopResult, Tool


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


def _make_state(bus: EventBus) -> RunState:
    return RunState(
        messages=[system_message("Be helpful."), user_message("Solve this.")],
        registry=ToolRegistry(tools=_tools(), event_bus=bus),
        event_bus=bus,
    )


class TestCodeExecStrategy:
    def test_code_strategy_has_name(self):
        from arcrun.strategies.code import CodeExecStrategy

        s = CodeExecStrategy()
        assert s.name == "code"

    def test_code_strategy_has_description(self):
        from arcrun.strategies.code import CodeExecStrategy

        s = CodeExecStrategy()
        assert isinstance(s.description, str)
        assert len(s.description) > 0

    def test_code_strategy_is_strategy_subclass(self):
        from arcrun.strategies import Strategy
        from arcrun.strategies.code import CodeExecStrategy

        assert issubclass(CodeExecStrategy, Strategy)

    @pytest.mark.asyncio
    async def test_call_augments_system_message(self):
        from arcrun.strategies.code import CodeExecStrategy

        model = MockModel([LLMResponse(content="Done.", stop_reason="end_turn")])
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)
        original_system = state.messages[0].content

        strategy = CodeExecStrategy()
        await strategy(model, state, sandbox, max_turns=5)

        # System message should have been augmented (longer than original)
        augmented = state.messages[0].content
        assert len(augmented) > len(original_system)

    @pytest.mark.asyncio
    async def test_call_delegates_to_react_loop(self):
        from arcrun.strategies.code import CodeExecStrategy

        model = MockModel([LLMResponse(content="Answer.", stop_reason="end_turn")])
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)

        strategy = CodeExecStrategy()
        result = await strategy(model, state, sandbox, max_turns=5)

        assert isinstance(result, LoopResult)
        assert result.content == "Answer."

    @pytest.mark.asyncio
    async def test_custom_prefix_override(self):
        from arcrun.strategies.code import CodeExecStrategy

        custom = "CUSTOM PREFIX: Use code."
        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)

        strategy = CodeExecStrategy(system_prompt_prefix=custom)
        await strategy(model, state, sandbox, max_turns=5)

        assert custom in state.messages[0].content

    @pytest.mark.asyncio
    async def test_emits_code_prompt_augmented_event(self):
        from arcrun.strategies.code import CodeExecStrategy

        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)

        strategy = CodeExecStrategy()
        await strategy(model, state, sandbox, max_turns=5)

        augmented_events = [e for e in bus.events if e.type == "code.prompt.augmented"]
        assert len(augmented_events) == 1
        assert "original_length" in augmented_events[0].data
        assert "augmented_length" in augmented_events[0].data
