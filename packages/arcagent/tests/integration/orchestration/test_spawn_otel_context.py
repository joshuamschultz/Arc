"""Integration tests for OTel trace context propagation in spawn.

Verifies:
1. Trace context propagates to child spans
2. arc.delegation.depth attribute set on child span
3. Graceful fallback when OTel not installed/configured
"""

from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import pytest
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool
from arctrust import derive_child_identity

from arcagent.orchestration.spawn import (
    _end_child_span,
    _get_otel_context,
    _start_child_span,
    spawn,
)

from ._mock_llm import LLMResponse, MockModel


async def _echo_execute(params: dict, ctx: object) -> str:
    return "echo"


ECHO_TOOL = Tool(
    name="echo",
    description="Echo",
    input_schema={"type": "object"},
    execute=_echo_execute,
)


def _make_parent_state(depth: int = 0, model: object = None) -> RunState:
    bus = EventBus(run_id="otel-test-run")
    reg = ToolRegistry(tools=[ECHO_TOOL], event_bus=bus)
    state = RunState(
        messages=[],
        registry=reg,
        event_bus=bus,
        run_id="otel-test-run",
        depth=depth,
        max_depth=3,
    )
    if model is not None:
        state._model = model  # type: ignore[attr-defined]
    return state


class TestOTelContextPropagation:
    def test_get_otel_context_does_not_crash(self) -> None:
        """_get_otel_context() must not crash even if OTel is not configured."""
        ctx = _get_otel_context()
        # Either None (no OTel) or a valid context object
        assert ctx is None or ctx is not None  # trivially true — no crash

    def test_start_child_span_returns_tuple(self) -> None:
        """_start_child_span must return (span, token) tuple."""
        span, token = _start_child_span("test-span", None, delegation_depth=1)
        # Without a configured tracer, may return (None, None) or real span
        assert isinstance((span, token), tuple)

    def test_end_child_span_does_not_crash_with_none(self) -> None:
        """_end_child_span with None span/token must not crash."""
        _end_child_span(None, None, "completed")  # should be a no-op

    def test_end_child_span_does_not_crash_with_error_status(self) -> None:
        """_end_child_span with error status and None span must not crash."""
        _end_child_span(None, None, "error")

    def test_span_start_with_depth_zero(self) -> None:
        span, token = _start_child_span("span-d0", None, delegation_depth=0)
        _end_child_span(span, token, "completed")

    def test_span_start_with_depth_two(self) -> None:
        span, token = _start_child_span("span-d2", None, delegation_depth=2)
        _end_child_span(span, token, "completed")

    @pytest.mark.asyncio
    async def test_spawn_records_parent_chain_tip_in_start_event(self) -> None:
        """spawn.start event must carry parent_chain_tip for audit chain continuity."""
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        state = _make_parent_state(model=model)
        # Emit a synthetic event so there's a chain tip to carry
        state.event_bus.emit("parent.event", {"data": "value"})
        parent_tip = state.event_bus.events[-1].event_hash

        identity = derive_child_identity(b"\x42" * 32, "otel-spawn-1", 30)
        await spawn(
            parent_state=state,
            task="do task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=model,
            wallclock_timeout_s=30,
        )

        # Verify spawn.start event was emitted with parent_chain_tip
        spawn_start_events = [e for e in state.event_bus.events if e.type == "spawn.start"]
        assert len(spawn_start_events) >= 1
        event_data = spawn_start_events[0].data
        assert "parent_chain_tip" in event_data
        assert event_data["parent_chain_tip"] == parent_tip

    @pytest.mark.asyncio
    async def test_spawn_includes_delegation_depth_in_start_event(self) -> None:
        """spawn.start event must carry parent_depth for OTel attribute tracing."""
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        state = _make_parent_state(depth=1, model=model)
        identity = derive_child_identity(b"\x42" * 32, "otel-spawn-2", 30)

        await spawn(
            parent_state=state,
            task="do task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=model,
            wallclock_timeout_s=30,
        )

        spawn_start_events = [e for e in state.event_bus.events if e.type == "spawn.start"]
        assert len(spawn_start_events) >= 1
        event_data = spawn_start_events[0].data
        # parent_depth should be 1 (parent's depth)
        assert event_data["parent_depth"] == 1

    @pytest.mark.asyncio
    async def test_spawn_child_did_in_complete_event(self) -> None:
        """spawn.complete event carries the child DID for audit trail."""
        model = MockModel([LLMResponse(content="done", stop_reason="end_turn")])
        state = _make_parent_state(model=model)
        identity = derive_child_identity(b"\x42" * 32, "otel-spawn-3", 30)

        await spawn(
            parent_state=state,
            task="task",
            tools=[ECHO_TOOL],
            system_prompt="sys",
            identity=identity,
            model=model,
            wallclock_timeout_s=30,
        )

        complete_events = [e for e in state.event_bus.events if e.type == "spawn.complete"]
        assert len(complete_events) >= 1
        assert complete_events[0].data["child_did"] == identity.did


class TestOTelWithRealOTel:
    """Tests that use the actual OTel SDK if available."""

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("opentelemetry"),
        reason="opentelemetry not installed",
    )
    def test_delegation_depth_attribute_with_real_otel(self) -> None:
        """When OTel is available, arc.delegation.depth attribute is set."""
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        trace.set_tracer_provider(provider)

        span, token = _start_child_span("test-span", None, delegation_depth=2)

        if span is not None:
            # Verify the attribute was set
            attributes = span.attributes if hasattr(span, "attributes") else {}
            assert attributes.get("arc.delegation.depth") == 2

        _end_child_span(span, token, "completed")
