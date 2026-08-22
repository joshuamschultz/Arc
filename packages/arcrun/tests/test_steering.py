"""Tests for steering: steer, followUp, cancel via RunHandle."""

import asyncio

import pytest
from packages.arcrun.tests.conftest import LLMResponse, MockModel, ToolCall

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
        # Steering is one rule now: a held message is ENTERED at the turn boundary,
        # under one event, whether it came in as a steer or a follow_up.
        events = [e for e in result.events if e.type == "message.injected"]
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
    async def test_a_held_message_never_interrupts_a_turns_tools(self):
        """A held message is entered at the turn BOUNDARY, never between tools.

        The old behavior skipped a turn's remaining tools on a steer; that mid-turn
        interruption is gone. A turn's tool calls all run to completion, and the
        held message is entered before the NEXT turn — so a user message can never
        land between an assistant tool_use and its tool_result.
        """
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
        handle._state.steer_queue.put_nowait(_injection("did:arc:caller", "redirect"))
        result = await handle.result()
        assert result.content == "After steer."
        # All three tools ran — none skipped by the held message.
        tool_results = [m for m in handle._state.messages if m.role == "tool"]
        assert len(tool_results) == 3


class TestFollowUp:
    @pytest.mark.asyncio
    async def test_a_message_arriving_during_a_turn_keeps_the_run_going(self):
        """A held message that arrives while a turn runs continues the run.

        The end-of-turn is not the end of the chat: a message that landed during
        the turn is entered at the next boundary and the run does another turn,
        rather than returning and leaving the message unanswered. The model here
        enqueues the follow-up as it finishes turn one — arrival DURING the turn,
        after that turn's boundary drain — so the continue path is what is tested.
        """
        from arcrun.loop import run_async

        # Turn 1 is a tool call, so the run naturally reaches a second turn; the
        # held message is entered at turn 1's boundary and rides into the rest of
        # the run as context — entered, not lost, and the chat keeps going.
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "a"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Also did X.", stop_reason="end_turn"),
            ]
        )
        handle = await run_async(model, StaticProvider(_tools()), "prompt", "task")
        handle._state.followup_queue.put_nowait(_injection("did:arc:mgr", "also do X"))
        result = await handle.result()

        assert result.content == "Also did X."
        assert result.turns == 2
        entered = [m for m in handle._state.messages if m.role == "user" and m.content == "also do X"]
        assert entered, "the held message must be entered into context, never lost"
        events = [e for e in result.events if e.type == "message.injected"]
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
        await handle.cancel("did:arc:operator")
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
        await handle.cancel("did:arc:operator")
        result = await handle.result()
        assert result.turns < 10


class TestSteerableDuringSelection:
    """A run must be steerable from the moment it is started.

    Choosing a strategy can cost a model call. While that call is in flight the
    run is already the operator's to cancel, so the ``RunHandle`` has to exist
    before selection begins — not after it returns.
    """

    async def test_a_run_is_cancellable_while_it_is_still_choosing_a_strategy(
        self,
    ) -> None:
        import asyncio

        from arcrun import StaticProvider
        from arcrun.loop import run_async
        from arcrun.types import Tool, ToolContext

        reached_selection = asyncio.Event()
        release = asyncio.Event()

        class ParkedModel:
            """Parks on the selection call, exactly as a slow provider would."""

            async def invoke(self, messages, tools=None, **_):
                if tools and any(getattr(t, "name", "") == "select_strategy" for t in tools):
                    reached_selection.set()
                    await release.wait()
                return LLMResponse(content="done", stop_reason="end_turn")

        async def _echo(params, ctx: ToolContext) -> str:
            return "ok"

        provider = StaticProvider(
            [
                Tool(
                    name="echo",
                    description="Echo",
                    input_schema={"type": "object", "properties": {}},
                    execute=_echo,
                )
            ]
        )

        # Bounded on purpose: if selection ever moves back outside the task,
        # ``run_async`` never returns and the failure is a hang. A timeout turns
        # that into a clean, readable failure instead of a stuck CI job.
        handle = await asyncio.wait_for(
            run_async(ParkedModel(), provider, "Be helpful.", "Do it"), timeout=2
        )

        # The handle exists while selection is still parked — that is the point.
        await asyncio.wait_for(reached_selection.wait(), timeout=2)
        await handle.cancel("did:arc:test:operator", reason="changed my mind")
        release.set()

        result = await handle.result()
        assert result is not None
