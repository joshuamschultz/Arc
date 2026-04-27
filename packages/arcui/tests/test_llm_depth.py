"""LLM call depth integration test.

Hard requirement: a test that exercises the full chain —
LLM call (mocked TraceRecord) → UIEvent emitted → browser client receives →
assert ALL user-asked fields are present and correct:
  request_body (with messages and tools)
  response_body (with content, tool_calls, usage)
  model
  provider
  duration_ms
  phase_timings (at least llm_call_ms)
  input_tokens
  output_tokens
  total_tokens
  cost_usd
  agent_did
  agent_label

No field silently dropped en route.
"""

from __future__ import annotations

import asyncio
import json

from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.reporter import UIEventReporter
from arcui.subscription import Subscription, SubscriptionManager


def _make_pipeline() -> tuple[EventBuffer, ConnectionManager, SubscriptionManager]:
    conn_mgr = ConnectionManager()
    sub_mgr = SubscriptionManager()
    buffer = EventBuffer(conn_mgr, subscription_manager=sub_mgr)
    return buffer, conn_mgr, sub_mgr


# ---------------------------------------------------------------------------
# Fixture: realistic TraceRecord-like data
# ---------------------------------------------------------------------------

_FULL_REQUEST_BODY = {
    "model": "claude-sonnet-4-6",
    "messages": [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Read /etc/hosts and summarize."},
    ],
    "tools": [
        {
            "name": "read_file",
            "description": "Read a file from the filesystem.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "list_dir",
            "description": "List directory contents.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    ],
    "max_tokens": 4096,
    "temperature": 0.3,
}

_FULL_RESPONSE_BODY = {
    "id": "msg_01XdfKmABCde123",
    "type": "message",
    "role": "assistant",
    "content": [
        {
            "type": "text",
            "text": "I'll read the file for you.",
        },
        {
            "type": "tool_use",
            "id": "toolu_01abc",
            "name": "read_file",
            "input": {"path": "/etc/hosts"},
        },
    ],
    "tool_calls": [
        {
            "id": "toolu_01abc",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "/etc/hosts"}',
            },
        }
    ],
    "usage": {
        "input_tokens": 250,
        "output_tokens": 75,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
    "stop_reason": "tool_use",
    "model": "claude-sonnet-4-6-20251001",
}


class TestLLMCallDepth:
    """Full chain: LLM call data → UIEvent → browser — no field dropped."""

    async def test_all_required_fields_present(self) -> None:
        """Emit a full LLM trace and verify every required field survives."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:executor/depth-agent-001",
            agent_name="depth-agent",
        )
        reporter.emit_llm_trace(
            model="claude-sonnet-4-6",
            provider="anthropic",
            duration_ms=1847.3,
            input_tokens=250,
            output_tokens=75,
            total_tokens=325,
            cost_usd=0.00412,
            phase_timings={
                "llm_call_ms": 1820.0,
                "serialization_ms": 15.3,
                "queue_wait_ms": 12.0,
            },
            request_body=_FULL_REQUEST_BODY,
            response_body=_FULL_RESPONSE_BODY,
            agent_did="did:arc:local:executor/depth-agent-001",
            agent_label="depth-agent",
        )
        buffer.flush_once()

        assert not queue.empty(), "UIEvent was not pushed to browser queue"
        raw = await asyncio.wait_for(queue.get(), timeout=2.0)
        event = json.loads(raw)
        payload = event["data"]

        # ── model / provider ──────────────────────────────────────────────
        assert payload.get("model") == "claude-sonnet-4-6", (
            f"model field wrong: {payload.get('model')!r}"
        )
        assert payload.get("provider") == "anthropic", (
            f"provider field wrong: {payload.get('provider')!r}"
        )

        # ── timing ────────────────────────────────────────────────────────
        assert payload.get("duration_ms") == 1847.3, (
            f"duration_ms field wrong: {payload.get('duration_ms')!r}"
        )
        assert "phase_timings" in payload, "phase_timings field missing"
        assert payload["phase_timings"].get("llm_call_ms") == 1820.0, (
            f"phase_timings.llm_call_ms wrong: {payload['phase_timings'].get('llm_call_ms')!r}"
        )

        # ── tokens ────────────────────────────────────────────────────────
        assert payload.get("input_tokens") == 250, (
            f"input_tokens wrong: {payload.get('input_tokens')!r}"
        )
        assert payload.get("output_tokens") == 75, (
            f"output_tokens wrong: {payload.get('output_tokens')!r}"
        )
        assert payload.get("total_tokens") == 325, (
            f"total_tokens wrong: {payload.get('total_tokens')!r}"
        )

        # ── cost ──────────────────────────────────────────────────────────
        assert payload.get("cost_usd") == 0.00412, (
            f"cost_usd wrong: {payload.get('cost_usd')!r}"
        )

        # ── request_body — messages + tools ──────────────────────────────
        req = payload.get("request_body")
        assert req is not None, "request_body field missing"
        assert "messages" in req, "request_body.messages missing"
        assert len(req["messages"]) == 2, "request_body.messages count wrong"
        assert "tools" in req, "request_body.tools missing"
        assert len(req["tools"]) == 2, "request_body.tools count wrong"
        assert req["tools"][0]["name"] == "read_file"

        # ── response_body — content + tool_calls + usage ─────────────────
        resp = payload.get("response_body")
        assert resp is not None, "response_body field missing"
        assert "content" in resp, "response_body.content missing"
        assert "tool_calls" in resp, "response_body.tool_calls missing"
        assert len(resp["tool_calls"]) == 1
        assert resp["tool_calls"][0]["function"]["name"] == "read_file"
        assert "usage" in resp, "response_body.usage missing"
        assert resp["usage"]["input_tokens"] == 250

        # ── agent identity ────────────────────────────────────────────────
        assert payload.get("agent_did") == "did:arc:local:executor/depth-agent-001", (
            f"agent_did wrong: {payload.get('agent_did')!r}"
        )
        assert payload.get("agent_label") == "depth-agent", (
            f"agent_label wrong: {payload.get('agent_label')!r}"
        )

    async def test_tool_calls_not_truncated(self) -> None:
        """Tool call data in response_body must not be truncated."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        multi_tool_response = {
            "content": "Using multiple tools",
            "tool_calls": [
                {"id": f"call_{i}", "function": {"name": f"tool_{i}", "arguments": f'{{"x": {i}}}'}}
                for i in range(5)
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

        reporter = UIEventReporter(
            event_buffer=buffer, agent_id="agent1", agent_name="test"
        )
        reporter.emit_llm_trace(
            model="gpt-4o",
            provider="openai",
            duration_ms=500.0,
            response_body=multi_tool_response,
        )
        buffer.flush_once()

        raw = await queue.get()
        payload = json.loads(raw)["data"]
        assert len(payload["response_body"]["tool_calls"]) == 5, (
            "tool_calls were truncated"
        )

    async def test_layer_is_llm(self) -> None:
        """LLM trace events must have layer='llm'."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        reporter = UIEventReporter(
            event_buffer=buffer, agent_id="agent1", agent_name="test"
        )
        reporter.emit_llm_trace(
            model="gpt-4o",
            provider="openai",
            duration_ms=100.0,
        )
        buffer.flush_once()

        raw = await queue.get()
        event = json.loads(raw)
        assert event["layer"] == "llm"

    async def test_phase_timings_all_keys_present(self) -> None:
        """All phase timing keys supplied survive to the browser without loss."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        all_timings = {
            "llm_call_ms": 1200.0,
            "serialization_ms": 15.0,
            "deserialization_ms": 8.0,
            "queue_wait_ms": 25.0,
            "retry_delay_ms": 0.0,
        }
        reporter = UIEventReporter(
            event_buffer=buffer, agent_id="agent1", agent_name="test"
        )
        reporter.emit_llm_trace(
            model="claude-opus-4",
            provider="anthropic",
            duration_ms=1248.0,
            phase_timings=all_timings,
        )
        buffer.flush_once()

        raw = await queue.get()
        payload = json.loads(raw)["data"]
        for key, value in all_timings.items():
            assert payload["phase_timings"].get(key) == value, (
                f"phase_timings.{key} missing or wrong"
            )


class TestLLMDepthViaTraceRecord:
    """Verify UIEventReporter.emit_from_trace_record() handles TraceRecord objects."""

    async def test_emit_from_trace_record(self) -> None:
        """TraceRecord → UIEvent full round-trip via emit_from_trace_record."""
        from arcllm.trace_store import TraceRecord

        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        # Build a realistic TraceRecord (frozen model, so no mutation needed)
        record = TraceRecord(
            provider="anthropic",
            model="claude-sonnet-4-6",
            agent_label="trace-agent",
            agent_did="did:arc:local:executor/trace-agent-001",
            request_body=_FULL_REQUEST_BODY,
            response_body=_FULL_RESPONSE_BODY,
            duration_ms=2100.0,
            cost_usd=0.0063,
            input_tokens=250,
            output_tokens=75,
            total_tokens=325,
            phase_timings={"llm_call_ms": 2050.0, "serialization_ms": 50.0},
            status="success",
        )

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:executor/trace-agent-001",
            agent_name="trace-agent",
        )
        reporter.emit_from_trace_record(record)
        buffer.flush_once()

        assert not queue.empty()
        raw = await asyncio.wait_for(queue.get(), timeout=2.0)
        event = json.loads(raw)
        payload = event["data"]

        assert payload["model"] == "claude-sonnet-4-6"
        assert payload["provider"] == "anthropic"
        assert payload["duration_ms"] == 2100.0
        assert payload["input_tokens"] == 250
        assert payload["output_tokens"] == 75
        assert payload["total_tokens"] == 325
        assert payload["cost_usd"] == 0.0063
        assert payload["phase_timings"]["llm_call_ms"] == 2050.0
        assert payload["request_body"]["tools"][0]["name"] == "read_file"
        assert payload["response_body"]["tool_calls"][0]["function"]["name"] == "read_file"
        assert payload["agent_did"] == "did:arc:local:executor/trace-agent-001"
        assert payload["agent_label"] == "trace-agent"
