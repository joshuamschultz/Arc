"""H-038 — per-call current-time context, injected fresh every model call.

The block must:
  * ride every model call's ``messages`` argument (not just the first turn);
  * never land in a ``system``-role message (SPEC-029 D-393: any system-role
    message is folded into the cache-control'd system blocks by the Anthropic
    adapter, so a per-call-varying value there busts the cached prefix every
    turn — the whole reason the hotfix says "not the system prompt");
  * never become part of the persisted transcript (``state.messages``) either,
    for the same reason one turn back — a value baked into the growing prefix
    would ride every later cached call too;
  * be resolved through an injectable ``state.clock`` (mirroring
    ``actor_did``/``run_origin``) rather than a hardcoded ``datetime.now()``,
    so a future replay/audit path can pin the exact recorded value;
  * be recorded on the hash-chained ``llm.call`` event, so what was actually
    injected is part of the tamper-evident trace;
  * round to minute granularity (no seconds — avoids cache/trace churn);
  * be marked ``ephemeral=True`` on the ``arcllm.Message`` itself — the flag
    ``arcllm.modules.routing`` (phrase match + tool-continuity lock) reads to
    skip it rather than mistake it for the newest real user turn or a broken
    tool-result run, since it always rides the literal last position.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from packages.arcrun.tests.conftest import LLMResponse, Message, MockModel, ToolCall

from arcrun._messages import time_context_message
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import Tool


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


def _tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=_echo_execute,
    )


def _make_state(bus: EventBus, *, clock=None) -> RunState:
    reg = ToolRegistry(tools=[_tool()], event_bus=bus)
    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Do the task."),
    ]
    return RunState(
        messages=messages,
        registry=reg,
        event_bus=bus,
        run_id="test-run",
        clock=clock,
    )


def _time_texts(messages: list) -> list[str]:
    """Every message whose content mentions the injected time block."""
    found = []
    for msg in messages:
        content = msg.content
        if isinstance(content, str) and "Current date/time:" in content:
            found.append(content)
    return found


class TestTimeContextInjection:
    def test_time_context_message_is_marked_ephemeral(self):
        """The routing-safety contract: ``ephemeral=True`` on the built message.

        ``arcllm.modules.routing._last_user_text`` / ``_trailing_tool_result_ids``
        skip a message with this flag rather than reading it as the newest real
        user turn or a broken tool-result run (both would otherwise silently
        derail phrase-based routing and the tool-continuity lock, since this
        block always rides the literal last position of every call).
        """
        msg = time_context_message(datetime(2026, 8, 29, 14, 32, 0, tzinfo=UTC))
        assert msg.ephemeral is True
        assert msg.role == "user"

    @pytest.mark.asyncio
    async def test_model_call_carries_current_time_block(self):
        bus = EventBus(run_id="test")
        fixed = datetime(2026, 8, 29, 14, 32, 7, tzinfo=UTC)
        state = _make_state(bus, clock=lambda: fixed)
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)

        sent = model.task_calls[0]["messages"]
        blocks = _time_texts(sent)
        assert len(blocks) == 1
        assert "2026-08-29 14:32" in blocks[0]

    @pytest.mark.asyncio
    async def test_time_block_truncates_to_minute_granularity(self):
        fixed_a = datetime(2026, 8, 29, 14, 32, 1, tzinfo=UTC)
        fixed_b = datetime(2026, 8, 29, 14, 32, 59, tzinfo=UTC)

        async def _run(fixed):
            b = EventBus(run_id="t")
            s = _make_state(b, clock=lambda: fixed)
            m = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
            await react_loop(m, s, Sandbox(config=None, event_bus=b), max_turns=5)
            return _time_texts(m.task_calls[0]["messages"])[0]

        text_a = await _run(fixed_a)
        text_b = await _run(fixed_b)
        assert text_a == text_b

    @pytest.mark.asyncio
    async def test_time_block_is_never_system_role(self):
        bus = EventBus(run_id="test")
        fixed = datetime(2026, 8, 29, 14, 32, 0, tzinfo=UTC)
        state = _make_state(bus, clock=lambda: fixed)
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)

        sent = model.task_calls[0]["messages"]
        for msg in sent:
            if "Current date/time:" in (msg.content if isinstance(msg.content, str) else ""):
                assert msg.role != "system"

    @pytest.mark.asyncio
    async def test_time_block_never_persisted_to_state_messages(self):
        """Ephemeral per-call block — must not ride the growing cached prefix."""
        bus = EventBus(run_id="test")
        fixed = datetime(2026, 8, 29, 14, 32, 0, tzinfo=UTC)
        state = _make_state(bus, clock=lambda: fixed)
        model = MockModel(
            [
                LLMResponse(content="turn one", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)

        assert _time_texts(state.messages) == []

    @pytest.mark.asyncio
    async def test_default_clock_used_when_none_injected(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus, clock=None)
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)

        before = datetime.now(UTC)
        await react_loop(model, state, sandbox, max_turns=5)
        after = datetime.now(UTC)

        sent = model.task_calls[0]["messages"]
        text = _time_texts(sent)[0]
        stamp = text.split("Current date/time:", 1)[1].strip()
        # Parse back "YYYY-MM-DD HH:MM <tz>" — just check the date/hour/minute
        # land inside the [before, after] window (minute-truncated).
        date_part = " ".join(stamp.split(" ")[:2])
        parsed = datetime.strptime(date_part, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        assert before.replace(second=0, microsecond=0) <= parsed <= after

    @pytest.mark.asyncio
    async def test_current_time_recorded_on_llm_call_event(self):
        """The resolved value lands in the hash-chained trace, not just the prompt."""
        bus = EventBus(run_id="test")
        fixed = datetime(2026, 8, 29, 14, 32, 0, tzinfo=UTC)
        state = _make_state(bus, clock=lambda: fixed)
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)

        llm_call_events = [e for e in bus.events if e.type == "llm.call"]
        assert len(llm_call_events) == 1
        assert "2026-08-29 14:32" in llm_call_events[0].data["current_time"]

    @pytest.mark.asyncio
    async def test_cross_turn_cached_prefix_is_byte_stable(self):
        """Two turns, minutes apart — the cached system/prefix segment must not
        move even though the injected time does (SPEC-029 D-393).

        Proof of two things at once:
          (a) the system-role segment (what an adapter folds into the
              cache_control'd ``system`` blocks) is byte-identical turn to
              turn, so it is never the carrier of the varying value;
          (b) everything turn 1 sent, minus its own trailing time block, is an
              exact untouched prefix of what turn 2 sends — the provider's
              cached prefix never changes shape or content turn-to-turn, only
              the per-turn tail (new conversation content + a fresh time
              block) grows after it.
        """
        bus = EventBus(run_id="test")
        times = iter(
            [
                datetime(2026, 8, 29, 14, 32, 0, tzinfo=UTC),
                datetime(2026, 8, 29, 14, 41, 0, tzinfo=UTC),  # +9 minutes
            ]
        )
        state = _make_state(bus, clock=lambda: next(times))
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc-1", name="echo", arguments={"input": "hi"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)

        assert len(model.task_calls) == 2
        turn1_messages = model.task_calls[0]["messages"]
        turn2_messages = model.task_calls[1]["messages"]

        # The two injected time blocks really differ — otherwise this test
        # would prove nothing.
        time1 = _time_texts(turn1_messages)[0]
        time2 = _time_texts(turn2_messages)[0]
        assert time1 != time2
        assert "14:32" in time1
        assert "14:41" in time2

        # (a) the cached SYSTEM segment is byte-identical across both calls.
        system1 = [m for m in turn1_messages if m.role == "system"]
        system2 = [m for m in turn2_messages if m.role == "system"]
        assert system1 == system2

        # (b) turn 1's messages, minus its own trailing time block, are an
        # exact, untouched prefix of turn 2's messages.
        stable_prefix = turn1_messages[:-1]
        assert turn2_messages[: len(stable_prefix)] == stable_prefix

    @pytest.mark.asyncio
    async def test_injected_clock_reproduces_identical_text_across_runs(self):
        """Same injected clock -> byte-identical text — the determinism seam.

        A future replay/audit tool pins ``state.clock`` to the value recorded on
        the ``llm.call`` event rather than calling ``now()`` again; this proves
        that path is fully deterministic (no hidden extra clock read).
        """
        fixed = datetime(2026, 8, 29, 14, 32, 0, tzinfo=UTC)

        async def _run():
            b = EventBus(run_id="t")
            s = _make_state(b, clock=lambda: fixed)
            m = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
            await react_loop(m, s, Sandbox(config=None, event_bus=b), max_turns=5)
            return _time_texts(m.task_calls[0]["messages"])[0]

        first = await _run()
        second = await _run()
        assert first == second
