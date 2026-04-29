"""End-to-end layer flow tests — all 4 layers: llm, run, agent, team.

Verifies that events on each layer flow correctly from emission through
EventBuffer → SubscriptionManager → browser queue with correct field values.

LLM layer: proves full TraceRecord depth (all user-required fields present).
Run/Agent/Team layers: tests use UIEventReporter directly (cross-package
UIReporter wiring not done — surfaced as cross-package issues).
"""

from __future__ import annotations

import asyncio
import json

import pytest

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
# LLM layer — FR-5, full field depth
# ---------------------------------------------------------------------------


class TestLLMLayer:
    """LLM layer events carry the full TraceRecord field set."""

    async def test_llm_event_reaches_browser(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:executor/agent1",
            agent_name="test-agent",
        )
        reporter.emit_llm_trace(
            model="claude-sonnet-4-6",
            provider="anthropic",
            duration_ms=1234.5,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=0.0025,
            phase_timings={"llm_call_ms": 1200.0, "serialization_ms": 34.5},
            request_body={
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "Hello"}],
                "tools": [{"name": "read_file", "description": "Read a file"}],
            },
            response_body={
                "id": "msg_123",
                "content": [{"type": "text", "text": "Hi there"}],
                "tool_calls": [
                    {"id": "call_1", "name": "read_file", "arguments": {"path": "/tmp/x"}}
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            agent_did="did:arc:local:executor/agent1",
            agent_label="test-agent",
        )
        buffer.flush_once()

        assert not queue.empty()
        raw = await asyncio.wait_for(queue.get(), timeout=1.0)
        data = json.loads(raw)

        assert data["layer"] == "llm"
        assert data["event_type"] == "llm_trace"

    async def test_llm_event_full_field_depth(self) -> None:
        """All user-required fields must survive the round-trip without truncation."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["llm"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:executor/agent1",
            agent_name="depth-agent",
        )
        reporter.emit_llm_trace(
            model="gpt-4o",
            provider="openai",
            duration_ms=987.6,
            input_tokens=200,
            output_tokens=80,
            total_tokens=280,
            cost_usd=0.0056,
            phase_timings={"llm_call_ms": 950.0, "queue_wait_ms": 37.6},
            request_body={
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "What tools do you have?"},
                ],
                "tools": [
                    {"name": "search", "description": "Web search"},
                    {"name": "read_file", "description": "File reader"},
                ],
            },
            response_body={
                "id": "chatcmpl-abc",
                "choices": [{"message": {"role": "assistant", "content": "I have 2 tools"}}],
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "search", "arguments": '{"q": "test"}'}}
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 80},
            },
            agent_did="did:arc:local:executor/agent1",
            agent_label="depth-agent",
        )
        buffer.flush_once()

        raw = await asyncio.wait_for(queue.get(), timeout=1.0)
        d = json.loads(raw)
        payload = d["data"]

        # All user-required fields must be present and correct
        assert payload["model"] == "gpt-4o", "model field missing or wrong"
        assert payload["provider"] == "openai", "provider field missing or wrong"
        assert payload["duration_ms"] == 987.6, "duration_ms field missing or wrong"
        assert payload["input_tokens"] == 200, "input_tokens field missing or wrong"
        assert payload["output_tokens"] == 80, "output_tokens field missing or wrong"
        assert payload["total_tokens"] == 280, "total_tokens field missing or wrong"
        assert payload["cost_usd"] == 0.0056, "cost_usd field missing or wrong"
        assert "phase_timings" in payload, "phase_timings field missing"
        assert payload["phase_timings"]["llm_call_ms"] == 950.0
        assert "request_body" in payload, "request_body field missing"
        assert payload["request_body"]["tools"][0]["name"] == "search"
        assert "response_body" in payload, "response_body field missing"
        assert payload["response_body"]["tool_calls"][0]["function"]["name"] == "search"
        assert "agent_did" in payload, "agent_did field missing"
        assert payload["agent_did"] == "did:arc:local:executor/agent1"
        assert "agent_label" in payload, "agent_label field missing"
        assert payload["agent_label"] == "depth-agent"

    async def test_llm_layer_subscription_filter(self) -> None:
        """Browser subscribed to 'run' should not receive 'llm' events."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        run_queue = conn_mgr.create_queue()
        llm_queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(run_queue, Subscription(layers=["run"]))
        sub_mgr.set_subscription(llm_queue, Subscription(layers=["llm"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="agent1",
            agent_name="test",
        )
        reporter.emit_llm_trace(
            model="gpt-4o",
            provider="openai",
            duration_ms=100.0,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost_usd=0.0001,
        )
        buffer.flush_once()

        assert not llm_queue.empty()
        assert run_queue.empty()


# ---------------------------------------------------------------------------
# Run layer — FR-5
# ---------------------------------------------------------------------------


class TestRunLayer:
    """Run layer events carry spawn/complete/stream signals."""

    async def test_run_spawn_event_reaches_browser(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["run"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:executor/agent1",
            agent_name="run-agent",
        )
        reporter.emit_run_event(
            event_type="spawn_start",
            data={"run_id": "run-abc-001", "goal": "summarize document"},
        )
        buffer.flush_once()

        assert not queue.empty()
        raw = await asyncio.wait_for(queue.get(), timeout=1.0)
        d = json.loads(raw)
        assert d["layer"] == "run"
        assert d["event_type"] == "spawn_start"
        assert d["data"]["run_id"] == "run-abc-001"

    async def test_run_complete_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["run"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="agent1",
            agent_name="run-agent",
        )
        reporter.emit_run_event(
            event_type="spawn_complete",
            data={"run_id": "run-abc-001", "status": "success", "duration_ms": 3500.0},
        )
        buffer.flush_once()

        raw = await queue.get()
        d = json.loads(raw)
        assert d["event_type"] == "spawn_complete"
        assert d["data"]["status"] == "success"

    async def test_run_stream_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["run"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="agent1",
            agent_name="run-agent",
        )
        reporter.emit_run_event(
            event_type="stream_token",
            data={"run_id": "run-abc-001", "token": "Hello"},
        )
        buffer.flush_once()

        raw = await queue.get()
        d = json.loads(raw)
        assert d["event_type"] == "stream_token"

    @pytest.mark.skip(reason="Waiting on arcrun UIReporter — cross-package issue")
    async def test_arcrun_uireporter_integration(self) -> None:
        """arcrun package needs to implement UIReporter that emits to arcui.

        Cross-package issue: arcrun does not yet have a UIReporter module.
        When arcrun's UIReporter is implemented, this test should be enabled
        and verify that arcrun's agentic loop emits run-layer UIEvents.
        """


# ---------------------------------------------------------------------------
# Agent layer — FR-5
# ---------------------------------------------------------------------------


class TestAgentLayer:
    """Agent layer events: tool calls, skill loads, extension loads, memory writes."""

    async def test_tool_call_event_reaches_browser(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["agent"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:executor/agent1",
            agent_name="agent-1",
        )
        reporter.emit_agent_event(
            event_type="tool_call",
            data={
                "tool_name": "read_file",
                "arguments": {"path": "/etc/hosts"},
                "outcome": "allow",
            },
        )
        buffer.flush_once()

        assert not queue.empty()
        raw = await asyncio.wait_for(queue.get(), timeout=1.0)
        d = json.loads(raw)
        assert d["layer"] == "agent"
        assert d["event_type"] == "tool_call"
        assert d["data"]["tool_name"] == "read_file"

    async def test_skill_load_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["agent"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="agent1",
            agent_name="agent-1",
        )
        reporter.emit_agent_event(
            event_type="skill_load",
            data={"skill_name": "code_review", "version": "1.2.0"},
        )
        buffer.flush_once()

        raw = await queue.get()
        d = json.loads(raw)
        assert d["event_type"] == "skill_load"
        assert d["data"]["skill_name"] == "code_review"

    async def test_extension_load_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["agent"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="agent1",
            agent_name="agent-1",
        )
        reporter.emit_agent_event(
            event_type="extension_load",
            data={"extension_name": "vault_extension", "status": "ok"},
        )
        buffer.flush_once()

        raw = await queue.get()
        d = json.loads(raw)
        assert d["event_type"] == "extension_load"

    async def test_memory_write_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["agent"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="agent1",
            agent_name="agent-1",
        )
        reporter.emit_agent_event(
            event_type="memory_write",
            data={"key": "context.md", "size_bytes": 1024},
        )
        buffer.flush_once()

        raw = await queue.get()
        d = json.loads(raw)
        assert d["event_type"] == "memory_write"
        assert d["data"]["key"] == "context.md"

    @pytest.mark.skip(reason="Waiting on arcagent UIReporter — cross-package issue")
    async def test_arcagent_uireporter_integration(self) -> None:
        """arcagent package needs to implement UIReporter module.

        Cross-package issue: arcagent does not yet have a UIReporter module
        that connects the agent's tool_registry / module_bus to arcui.
        When implemented, this test verifies real tool call events flow
        from arcagent's tool registry into arcui's agent layer.
        """


# ---------------------------------------------------------------------------
# Team layer — FR-5
# ---------------------------------------------------------------------------


class TestTeamLayer:
    """Team layer events: entity register, message routing."""

    async def test_entity_register_event_reaches_browser(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["team"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:team/orchestrator",
            agent_name="team-orchestrator",
        )
        reporter.emit_team_event(
            event_type="entity_register",
            data={
                "entity_id": "did:arc:local:executor/worker-1",
                "role": "worker",
                "team": "research-team",
            },
        )
        buffer.flush_once()

        assert not queue.empty()
        raw = await asyncio.wait_for(queue.get(), timeout=1.0)
        d = json.loads(raw)
        assert d["layer"] == "team"
        assert d["event_type"] == "entity_register"
        assert d["data"]["entity_id"] == "did:arc:local:executor/worker-1"

    async def test_message_routing_event(self) -> None:
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription(layers=["team"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="did:arc:local:team/orchestrator",
            agent_name="team-orchestrator",
        )
        reporter.emit_team_event(
            event_type="message_route",
            data={
                "from_id": "did:arc:local:executor/worker-1",
                "to_id": "did:arc:local:executor/worker-2",
                "message_type": "task_delegate",
                "team": "research-team",
            },
        )
        buffer.flush_once()

        raw = await queue.get()
        d = json.loads(raw)
        assert d["event_type"] == "message_route"
        assert d["data"]["team"] == "research-team"

    async def test_team_filter_excludes_other_teams(self) -> None:
        """Team subscription filter: only events from the subscribed team arrive."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        research_queue = conn_mgr.create_queue()
        ops_queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(
            research_queue, Subscription(layers=["team"], teams=["research-team"])
        )
        sub_mgr.set_subscription(ops_queue, Subscription(layers=["team"], teams=["ops-team"]))

        reporter = UIEventReporter(
            event_buffer=buffer,
            agent_id="orchestrator",
            agent_name="orch",
        )
        reporter.emit_team_event(
            event_type="message_route",
            data={"team": "research-team", "msg": "hello"},
        )
        buffer.flush_once()

        assert not research_queue.empty(), "Research queue should receive the event"
        assert ops_queue.empty(), "Ops queue should not receive research-team events"

    @pytest.mark.skip(reason="Waiting on arcteam UIReporter — cross-package issue")
    async def test_arcteam_uireporter_integration(self) -> None:
        """arcteam package needs to implement UIReporter module.

        Cross-package issue: arcteam does not yet have a UIReporter module
        that emits entity_register / message_route events into arcui.
        """


# ---------------------------------------------------------------------------
# Multi-layer: multiple layers flow independently
# ---------------------------------------------------------------------------


class TestMultiLayerIndependence:
    """Events on different layers do not interfere with each other."""

    async def test_four_layers_simultaneously(self) -> None:
        """Events on all 4 layers arrive at the correct layer-filtered queues."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        llm_q = conn_mgr.create_queue()
        run_q = conn_mgr.create_queue()
        agent_q = conn_mgr.create_queue()
        team_q = conn_mgr.create_queue()
        all_q = conn_mgr.create_queue()

        sub_mgr.set_subscription(llm_q, Subscription(layers=["llm"]))
        sub_mgr.set_subscription(run_q, Subscription(layers=["run"]))
        sub_mgr.set_subscription(agent_q, Subscription(layers=["agent"]))
        sub_mgr.set_subscription(team_q, Subscription(layers=["team"]))
        sub_mgr.set_subscription(all_q, Subscription())

        reporter = UIEventReporter(event_buffer=buffer, agent_id="agent1", agent_name="test")
        reporter.emit_llm_trace(model="gpt-4o", provider="openai", duration_ms=100.0)
        reporter.emit_run_event(event_type="spawn_start", data={"run_id": "r1"})
        reporter.emit_agent_event(event_type="tool_call", data={"tool": "x"})
        reporter.emit_team_event(event_type="entity_register", data={"entity": "e1"})
        buffer.flush_once()

        # Each layer queue should have exactly 1 event
        assert llm_q.qsize() == 1
        assert run_q.qsize() == 1
        assert agent_q.qsize() == 1
        assert team_q.qsize() == 1
        # All-subscriber should have 4 events
        assert all_q.qsize() == 4

    async def test_cross_layer_no_contamination(self) -> None:
        """LLM events should not contain run-layer fields and vice versa."""
        buffer, conn_mgr, sub_mgr = _make_pipeline()
        queue = conn_mgr.create_queue()
        sub_mgr.set_subscription(queue, Subscription())

        reporter = UIEventReporter(event_buffer=buffer, agent_id="agent1", agent_name="test")
        reporter.emit_llm_trace(model="gpt-4o", provider="openai", duration_ms=100.0)
        reporter.emit_run_event(event_type="spawn_start", data={"run_id": "r1"})
        buffer.flush_once()

        events = []
        while not queue.empty():
            events.append(json.loads(await queue.get()))

        layers = {e["layer"] for e in events}
        assert layers == {"llm", "run"}
