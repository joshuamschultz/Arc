"""Integration test: load_model() with trace_store + on_event → 3 calls → verify JSONL + chain.

Task 1.8 — end-to-end integration of TraceStore, on_event, and TelemetryModule.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.modules.telemetry import TelemetryModule
from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_RESPONSES = [
    LLMResponse(
        content="response-1",
        usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
        model="claude-sonnet-4",
        stop_reason="end_turn",
    ),
    LLMResponse(
        content="response-2",
        usage=Usage(
            input_tokens=200,
            output_tokens=80,
            total_tokens=280,
            cache_read_tokens=50,
        ),
        model="claude-sonnet-4",
        stop_reason="end_turn",
    ),
    LLMResponse(
        content="response-3",
        usage=Usage(input_tokens=300, output_tokens=120, total_tokens=420),
        model="claude-sonnet-4",
        stop_reason="max_tokens",
    ),
]


def _make_inner() -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = "anthropic"
    inner.model_name = "claude-sonnet-4"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(side_effect=_RESPONSES)
    return inner


class TestTraceIntegration:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        return tmp_path / "workspace"

    async def test_full_trace_pipeline(self, workspace: Path):
        """3 invoke calls → JSONL has 3 records → chain verifies → query returns all 3."""
        store = JSONLTraceStore(workspace)
        events: list[TraceRecord] = []

        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "cost_cache_read_per_1m": 0.30,
            "cost_cache_write_per_1m": 3.75,
            "trace_store": store,
            "on_event": events.append,
            "agent_label": "test-agent",
        }
        module = TelemetryModule(config, inner)

        messages = [Message(role="user", content="hello")]
        for _ in range(3):
            await module.invoke(messages)

        # on_event fired 3 times
        assert len(events) == 3

        # All events have correct provider/model/agent_label
        for rec in events:
            assert rec.provider == "anthropic"
            assert rec.model == "claude-sonnet-4"
            assert rec.agent_label == "test-agent"
            assert rec.status == "success"
            assert rec.duration_ms > 0 or rec.duration_ms == 0.0  # May be very fast

        # Phase timings present on all records
        for rec in events:
            assert "prompt_assembly_ms" in rec.phase_timings
            assert "llm_call_ms" in rec.phase_timings
            assert "post_processing_ms" in rec.phase_timings
            assert "total_ms" in rec.phase_timings

        # Cost calculated correctly for first call: (100*3 + 50*15) / 1e6 = 0.00105
        assert abs(events[0].cost_usd - 0.001050) < 1e-9

        # Token counts match responses
        assert events[0].input_tokens == 100
        assert events[1].input_tokens == 200
        assert events[2].input_tokens == 300
        assert events[2].stop_reason == "max_tokens"

        # Hash chain verifies
        assert await store.verify_chain() is True

        # Query returns all 3 (newest first)
        results, _cursor = await store.query(limit=10)
        assert len(results) == 3

        # Query by agent filter
        agent_results, _ = await store.query(agent="test-agent")
        assert len(agent_results) == 3

        # Request/response bodies stored (default store_raw_bodies=True)
        for rec in events:
            assert rec.request_body is not None
            assert rec.response_body is not None

    async def test_trace_pipeline_without_raw_bodies(self, workspace: Path):
        """store_raw_bodies=False omits request/response bodies."""
        store = JSONLTraceStore(workspace)
        events: list[TraceRecord] = []

        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "trace_store": store,
            "on_event": events.append,
            "store_raw_bodies": False,
        }
        module = TelemetryModule(config, inner)

        messages = [Message(role="user", content="hello")]
        await module.invoke(messages)

        rec = events[0]
        assert rec.request_body is None
        assert rec.response_body is None
        # But telemetry data still present
        assert rec.cost_usd > 0
        assert rec.input_tokens == 100

    async def test_trace_store_only_no_callback(self, workspace: Path):
        """trace_store works without on_event callback."""
        store = JSONLTraceStore(workspace)

        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "trace_store": store,
        }
        module = TelemetryModule(config, inner)

        messages = [Message(role="user", content="hello")]
        await module.invoke(messages)

        results, _ = await store.query()
        assert len(results) == 1
        assert await store.verify_chain() is True

    async def test_callback_only_no_store(self):
        """on_event works without trace_store."""
        events: list[TraceRecord] = []

        inner = _make_inner()
        config = {
            "cost_input_per_1m": 3.00,
            "cost_output_per_1m": 15.00,
            "on_event": events.append,
        }
        module = TelemetryModule(config, inner)

        messages = [Message(role="user", content="hello")]
        await module.invoke(messages)

        assert len(events) == 1
