"""GAP-B: an operator cancel is attributable and yields a structured terminator.

``RunHandle.cancel(caller_did, reason)`` must record who pulled the kill switch
(ASI09/ASI10) and route the halt through the same structured-terminator shape as
a budget breach — a ``task_complete``-style payload with ``error="cancelled"``, a
human-visible partial naming the operator, and a ``loop.cancelled`` audit event.
"""

import asyncio

import pytest
from conftest import LLMResponse, MockModel, ToolCall

from arcrun import StaticProvider
from arcrun.types import Tool


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


def _looping_model() -> MockModel:
    return MockModel(
        [
            LLMResponse(
                tool_calls=[ToolCall(id=f"tc{i}", name="echo", arguments={"input": "x"})],
                stop_reason="tool_use",
            )
            for i in range(20)
        ]
    )


class TestCancelAttribution:
    @pytest.mark.asyncio
    async def test_cancel_requires_caller_did(self):
        from arcrun.loop import run_async

        model = MockModel([LLMResponse(content="ok", stop_reason="end_turn")])
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        with pytest.raises(ValueError, match="caller_did"):
            await handle.cancel("")
        await handle.result()

    @pytest.mark.asyncio
    async def test_cancel_records_caller_on_state(self):
        from arcrun.loop import run_async

        handle = await run_async(
            _looping_model(), StaticProvider(_tools()), "prompt", "task", max_turns=20
        )
        await asyncio.sleep(0.02)
        await handle.cancel("did:arc:operator", reason="taking too long")
        await handle.result()
        assert handle.state.cancelled_by == "did:arc:operator"
        assert handle.state.cancel_reason == "taking too long"

    @pytest.mark.asyncio
    async def test_cancel_emits_structured_terminator(self):
        from arcrun.loop import run_async

        handle = await run_async(
            _looping_model(), StaticProvider(_tools()), "prompt", "task", max_turns=20
        )
        await asyncio.sleep(0.02)
        await handle.cancel("did:arc:operator", reason="taking too long")
        result = await handle.result()

        # Structured completion payload, mirroring a budget breach.
        assert result.completion_payload is not None
        assert result.completion_payload["error"] == "cancelled"
        # Human-visible partial names the operator and the reason.
        assert result.content is not None
        assert "did:arc:operator" in result.content
        assert "taking too long" in result.content

    @pytest.mark.asyncio
    async def test_cancel_emits_attributed_audit_event(self):
        from arcrun.loop import run_async

        handle = await run_async(
            _looping_model(), StaticProvider(_tools()), "prompt", "task", max_turns=20
        )
        await asyncio.sleep(0.02)
        await handle.cancel("did:arc:operator", reason="stop")
        result = await handle.result()

        cancelled = [e for e in result.events if e.type == "loop.cancelled"]
        assert cancelled, "expected a loop.cancelled audit event"
        data = cancelled[0].data
        assert data["caller_did"] == "did:arc:operator"
        assert data["reason"] == "stop"
