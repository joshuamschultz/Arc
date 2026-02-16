"""Tests for run() and run_async() entry points."""
import pytest

from arcrun.types import Tool

from conftest import LLMResponse, MockModel, ToolCall


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


class TestRun:
    @pytest.mark.asyncio
    async def test_end_to_end(self):
        from arcrun.loop import run

        model = MockModel([LLMResponse(content="Hello!", stop_reason="end_turn")])
        result = await run(model, _tools(), "Be helpful.", "Say hello")
        assert result.content == "Hello!"
        assert result.turns == 1
        assert result.strategy_used == "react"

    @pytest.mark.asyncio
    async def test_with_on_event(self):
        from arcrun.loop import run

        events_received = []
        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        result = await run(
            model, _tools(), "prompt", "task", on_event=lambda e: events_received.append(e)
        )
        assert len(events_received) > 0
        assert any(e.type == "loop.start" for e in events_received)

    @pytest.mark.asyncio
    async def test_strategy_selected_event_emitted(self):
        from arcrun.loop import run

        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        result = await run(model, _tools(), "prompt", "task")
        selected = [e for e in result.events if e.type == "strategy.selected"]
        assert len(selected) == 1
        assert selected[0].data["strategy"] == "react"

    @pytest.mark.asyncio
    async def test_unknown_strategy_raises(self):
        from arcrun.loop import run

        model = MockModel([])
        with pytest.raises(ValueError, match="unknown strategies"):
            await run(model, _tools(), "prompt", "task", allowed_strategies=["nonexistent"])

    @pytest.mark.asyncio
    async def test_with_sandbox(self):
        from arcrun.loop import run
        from arcrun.types import SandboxConfig

        model = MockModel([
            LLMResponse(
                tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "x"})],
                stop_reason="tool_use",
            ),
            LLMResponse(content="Denied.", stop_reason="end_turn"),
        ])
        cfg = SandboxConfig(allowed_tools=["other"])
        result = await run(model, _tools(), "prompt", "task", sandbox=cfg)
        denied = [e for e in result.events if e.type == "tool.denied"]
        assert len(denied) == 1

    @pytest.mark.asyncio
    async def test_with_transform_context(self):
        from arcrun.loop import run

        calls = []

        def transform(msgs):
            calls.append(True)
            return msgs

        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        await run(model, _tools(), "prompt", "task", transform_context=transform)
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_empty_tools_raises(self):
        from arcrun.loop import run

        model = MockModel([])
        with pytest.raises(ValueError, match="tools"):
            await run(model, [], "prompt", "task")

    @pytest.mark.asyncio
    async def test_model_error_bubbles(self):
        from arcrun.loop import run

        class BadModel:
            async def invoke(self, messages, tools=None):
                raise ConnectionError("API down")

        with pytest.raises(ConnectionError, match="API down"):
            await run(BadModel(), _tools(), "prompt", "task")


class TestRunWithMessages:
    """Tests for run() with the messages parameter (session history)."""

    @pytest.mark.asyncio
    async def test_messages_none_uses_default_behavior(self):
        """messages=None should produce [system, user] as before."""
        from arcrun.loop import run

        model = MockModel([LLMResponse(content="Hi!", stop_reason="end_turn")])
        result = await run(model, _tools(), "Be helpful.", "Say hello", messages=None)
        assert result.content == "Hi!"
        # Model received [system, user] (list is mutated after invoke, so check first two)
        invoke_msgs = model.invoke_calls[0]["messages"]
        assert invoke_msgs[0].role == "system"
        assert invoke_msgs[0].content == "Be helpful."
        assert invoke_msgs[1].role == "user"
        assert invoke_msgs[1].content == "Say hello"

    @pytest.mark.asyncio
    async def test_messages_provided_uses_history(self):
        """When messages provided, system prompt is prepended fresh."""
        from arcrun.loop import run
        from arcrun._messages import Message

        history = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there"),
            Message(role="user", content="how are you"),
        ]

        model = MockModel([LLMResponse(content="I'm good!", stop_reason="end_turn")])
        result = await run(
            model, _tools(), "Be helpful.", "how are you", messages=history,
        )
        assert result.content == "I'm good!"
        # Model received: system + history (3 msgs) — check structure
        invoke_msgs = model.invoke_calls[0]["messages"]
        assert invoke_msgs[0].role == "system"
        assert invoke_msgs[0].content == "Be helpful."
        # History messages follow system prompt
        assert invoke_msgs[1].role == "user"
        assert invoke_msgs[1].content == "hello"
        assert invoke_msgs[2].role == "assistant"
        assert invoke_msgs[3].role == "user"
        assert invoke_msgs[3].content == "how are you"

    @pytest.mark.asyncio
    async def test_messages_system_prompt_always_fresh(self):
        """System prompt is always rebuilt, not carried from old messages."""
        from arcrun.loop import run
        from arcrun._messages import Message

        # History includes an old system message — it should be ignored
        # because run() prepends a fresh system message
        history = [
            Message(role="system", content="OLD system prompt"),
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ]

        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        result = await run(
            model, _tools(), "NEW system prompt", "hello", messages=history,
        )
        # First message should be the fresh system prompt
        invoke_msgs = model.invoke_calls[0]["messages"]
        assert invoke_msgs[0].role == "system"
        assert invoke_msgs[0].content == "NEW system prompt"

    @pytest.mark.asyncio
    async def test_messages_empty_list_still_adds_system(self):
        """Empty messages list should still prepend system prompt."""
        from arcrun.loop import run

        model = MockModel([LLMResponse(content="OK", stop_reason="end_turn")])
        result = await run(
            model, _tools(), "System.", "task", messages=[],
        )
        invoke_msgs = model.invoke_calls[0]["messages"]
        assert invoke_msgs[0].role == "system"
        assert invoke_msgs[0].content == "System."

    @pytest.mark.asyncio
    async def test_run_async_with_messages(self):
        """run_async() also accepts messages parameter."""
        from arcrun.loop import run_async
        from arcrun._messages import Message

        history = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ]

        model = MockModel([LLMResponse(content="Done.", stop_reason="end_turn")])
        handle = await run_async(
            model, _tools(), "prompt", "task", messages=history,
        )
        result = await handle.result()
        assert result.content == "Done."


class TestRunAsync:
    @pytest.mark.asyncio
    async def test_returns_run_handle(self):
        from arcrun.loop import run_async, RunHandle

        model = MockModel([LLMResponse(content="Done.", stop_reason="end_turn")])
        handle = await run_async(model, _tools(), "prompt", "task")
        assert isinstance(handle, RunHandle)

    @pytest.mark.asyncio
    async def test_handle_result(self):
        from arcrun.loop import run_async

        model = MockModel([LLMResponse(content="Result.", stop_reason="end_turn")])
        handle = await run_async(model, _tools(), "prompt", "task")
        result = await handle.result()
        assert result.content == "Result."
