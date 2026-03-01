"""Integration test: full ArcLLM stack — CircuitBreaker + ConfigController + TraceStore + on_event.

Task 2.7 — End-to-end: make calls, trigger circuit break, patch config, verify all events in JSONL.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.config_controller import ConfigController
from arcllm.exceptions import ArcLLMAPIError
from arcllm.modules.circuit_breaker import CircuitBreakerModule, CircuitOpenError
from arcllm.modules.telemetry import TelemetryModule
from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="claude-sonnet-4",
    stop_reason="end_turn",
)


def _make_inner(name: str = "anthropic") -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = name
    inner.model_name = "claude-sonnet-4"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


class TestPhase2Integration:
    """Full stack: CircuitBreaker wrapping inner, TelemetryModule wrapping that,
    ConfigController emitting events, all events flowing to TraceStore + on_event."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        return tmp_path / "phase2"

    async def test_full_stack_calls_and_circuit_break(self, workspace: Path):
        """Make calls → trigger circuit break → verify all events in JSONL."""
        store = JSONLTraceStore(workspace)
        events: list[TraceRecord] = []

        def on_event(rec: TraceRecord) -> None:
            events.append(rec)

        # Build stack: TelemetryModule → CircuitBreakerModule → inner
        inner = _make_inner()
        cb = CircuitBreakerModule(
            {
                "failure_threshold": 2,
                "cooldown_seconds": 100.0,
                "on_event": on_event,
            },
            inner,
        )
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "trace_store": store,
                "on_event": on_event,
                "agent_label": "integration-agent",
            },
            cb,
        )

        messages = [Message(role="user", content="hello")]

        # Successful call
        result = await telemetry.invoke(messages)
        assert result.content == "ok"

        # Verify llm_call event in JSONL
        records, _ = await store.query(limit=10)
        assert len(records) == 1
        assert records[0].event_type == "llm_call"
        assert records[0].agent_label == "integration-agent"

        # Now cause failures to trip circuit breaker
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "server error", "anthropic")
        )

        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await telemetry.invoke(messages)

        # Circuit should be open
        assert cb.get_state()["state"] == "open"

        # on_event should have: 1 llm_call success + circuit_change closed→open
        # (error calls don't produce trace records from telemetry since they raise)
        circuit_events = [e for e in events if e.event_type == "circuit_change"]
        assert len(circuit_events) >= 1
        assert circuit_events[0].event_data["old_state"] == "closed"
        assert circuit_events[0].event_data["new_state"] == "open"

        # Next call should be rejected by circuit breaker
        with pytest.raises(CircuitOpenError):
            await telemetry.invoke(messages)

    async def test_config_controller_events_in_jsonl(self, workspace: Path):
        """ConfigController.patch() emits config_change events to JSONL."""
        store = JSONLTraceStore(workspace)
        events: list[TraceRecord] = []

        def on_event(rec: TraceRecord) -> None:
            events.append(rec)

        ctrl = ConfigController(
            {"model": "claude-sonnet-4", "temperature": 0.7},
            on_event=on_event,
        )

        # Patch temperature
        new = ctrl.patch({"temperature": 0.3}, actor="operator-1")
        assert new.temperature == 0.3

        # Verify config_change event
        assert len(events) == 1
        rec = events[0]
        assert rec.event_type == "config_change"
        assert rec.event_data["actor"] == "operator-1"
        assert "temperature" in rec.event_data["changes"]
        assert rec.event_data["changes"]["temperature"]["old"] == 0.7
        assert rec.event_data["changes"]["temperature"]["new"] == 0.3

        # Write to store manually (ConfigController doesn't own a store)
        await store.append(rec)

        # Verify it's in JSONL
        results, _ = await store.query(limit=10)
        assert len(results) == 1
        assert results[0].event_type == "config_change"

    async def test_mixed_events_in_single_store(self, workspace: Path):
        """LLM calls, circuit changes, and config changes all coexist in one JSONL."""
        store = JSONLTraceStore(workspace)
        events: list[TraceRecord] = []

        def on_event(rec: TraceRecord) -> None:
            events.append(rec)

        # Build stack
        inner = _make_inner()
        cb = CircuitBreakerModule(
            {
                "failure_threshold": 2,
                "cooldown_seconds": 100.0,
                "on_event": on_event,
            },
            inner,
        )
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "trace_store": store,
                "on_event": on_event,
            },
            cb,
        )

        ctrl = ConfigController(
            {"model": "claude-sonnet-4"},
            on_event=on_event,
        )

        messages = [Message(role="user", content="test")]

        # 1. Successful LLM call
        await telemetry.invoke(messages)

        # 2. Config change
        ctrl.patch({"temperature": 0.5}, actor="admin")

        # 3. Trigger circuit break
        inner.invoke = AsyncMock(
            side_effect=ArcLLMAPIError(500, "fail", "anthropic")
        )
        for _ in range(2):
            with pytest.raises(ArcLLMAPIError):
                await telemetry.invoke(messages)

        # Collect event types
        event_types = [e.event_type for e in events]
        assert "llm_call" in event_types
        assert "config_change" in event_types
        assert "circuit_change" in event_types

        # Store has the llm_call record (telemetry writes to store)
        results, _ = await store.query(limit=10)
        assert any(r.event_type == "llm_call" for r in results)

        # Chain still verifies
        assert await store.verify_chain() is True

    async def test_budget_state_queryable_in_stack(self):
        """get_budget_state() returns accurate data after invoke() calls."""
        inner = _make_inner()
        telemetry = TelemetryModule(
            {
                "cost_input_per_1m": 3.00,
                "cost_output_per_1m": 15.00,
                "budget_scope": "agent:integration",
                "monthly_limit_usd": 100.0,
                "daily_limit_usd": 10.0,
            },
            inner,
        )

        messages = [Message(role="user", content="hello")]

        # Before any calls
        state = telemetry.get_budget_state()
        assert state is not None
        assert state["monthly_spend"] == 0.0

        # After 2 calls
        await telemetry.invoke(messages)
        await telemetry.invoke(messages)

        state = telemetry.get_budget_state()
        assert state is not None
        # 2 calls * 0.001050 each
        expected = 0.001050 * 2
        assert abs(state["monthly_spend"] - expected) < 1e-9
        assert abs(state["daily_spend"] - expected) < 1e-9
        assert state["scope"] == "agent:integration"
        assert state["monthly_limit"] == 100.0
        assert state["daily_limit"] == 10.0
