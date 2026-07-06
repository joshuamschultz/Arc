"""Tests for steering: steer, followUp, cancel via RunHandle."""

import asyncio

import pytest
from conftest import LLMResponse, MockModel, ToolCall

from arcrun import StaticProvider
from arcrun.state import Injection
from arcrun.types import Tool


def _injection(caller_did: str, message: str) -> Injection:
    return Injection.new(caller_did, message)


async def _slow_echo(params: dict, ctx: object) -> str:
    await asyncio.sleep(0.01)
    return f"echo: {params.get('input', '')}"


def _tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_slow_echo,
        )
    ]


class TestSteer:
    @pytest.mark.asyncio
    async def test_steer_injects_message(self):
        from arcrun.loop import run_async

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "a"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Steered!", stop_reason="end_turn"),
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        # Give loop time to start, then steer
        await asyncio.sleep(0.005)
        await handle.steer("did:arc:caller", "change direction")
        result = await handle.result()
        assert result.content == "Steered!"

    @pytest.mark.asyncio
    async def test_steer_requires_caller_did(self):
        from arcrun.loop import run_async

        model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        with pytest.raises(ValueError, match="caller_did"):
            await handle.steer("", "change direction")
        await handle.result()

    @pytest.mark.asyncio
    async def test_steer_emits_attributed_audit_event(self):
        from arcrun.loop import run_async

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "a"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Steered!", stop_reason="end_turn"),
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        handle._state.steer_queue.put_nowait(_injection("did:arc:mgr", "redirect"))
        result = await handle.result()
        events = [e for e in result.events if e.type == "steer.injected"]
        assert len(events) == 1
        assert events[0].data["caller_did"] == "did:arc:mgr"
        assert events[0].data["preview"] == "redirect"
        assert events[0].data["message_id"]
        # Injected content is user-role data, never system.
        user_msgs = [m for m in handle._state.messages if m.role == "user"]
        assert any(m.content == "redirect" for m in user_msgs)
        assert not any(
            m.role == "system" and m.content == "redirect" for m in handle._state.messages
        )

    @pytest.mark.asyncio
    async def test_steer_skips_remaining_tools(self):
        from arcrun.loop import run_async

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="tc1", name="echo", arguments={"input": "1"}),
                        ToolCall(id="tc2", name="echo", arguments={"input": "2"}),
                        ToolCall(id="tc3", name="echo", arguments={"input": "3"}),
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="After steer.", stop_reason="end_turn"),
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        # Inject steer immediately so it catches between tools
        handle._state.steer_queue.put_nowait(_injection("did:arc:caller", "redirect"))
        result = await handle.result()
        assert result.content == "After steer."


class TestFollowUp:
    @pytest.mark.asyncio
    async def test_followup_continues_loop(self):
        from arcrun.loop import run_async

        model = MockModel(
            [
                LLMResponse(content="First answer.", stop_reason="end_turn"),
                LLMResponse(content="Also did X.", stop_reason="end_turn"),
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        # Queue followup before loop starts
        handle._state.followup_queue.put_nowait(_injection("did:arc:mgr", "also do X"))
        result = await handle.result()
        assert result.content == "Also did X."
        assert result.turns == 2
        events = [e for e in result.events if e.type == "followup.injected"]
        assert len(events) == 1
        assert events[0].data["caller_did"] == "did:arc:mgr"
        assert events[0].data["preview"] == "also do X"
        assert events[0].data["message_id"]

    @pytest.mark.asyncio
    async def test_followup_empty_returns_normally(self):
        from arcrun.loop import run_async

        model = MockModel([LLMResponse(content="Done.", stop_reason="end_turn")])
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        result = await handle.result()
        assert result.content == "Done."
        assert result.turns == 1


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_sets_event(self):
        from arcrun.loop import run_async

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                )
                for _ in range(10)
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task", max_turns=10)
        await asyncio.sleep(0.02)
        await handle.cancel()
        result = await handle.result()
        assert handle.state.cancel_event.is_set()
        assert isinstance(result, type(result))  # got a result, not an exception

    @pytest.mark.asyncio
    async def test_cancel_returns_partial_result(self):
        from arcrun.loop import run_async

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id=f"tc{i}", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                )
                for i in range(10)
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task", max_turns=10)
        await asyncio.sleep(0.02)
        await handle.cancel()
        result = await handle.result()
        assert result.turns < 10
