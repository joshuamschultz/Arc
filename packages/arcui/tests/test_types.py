"""Tests for UIEvent, ControlMessage, ControlResponse, AgentRegistration models."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from arcui.types import AgentRegistration, ControlMessage, ControlResponse, UIEvent


class TestUIEvent:
    def test_valid_llm_event(self):
        event = UIEvent(
            layer="llm",
            event_type="trace_record",
            agent_id="agent-001",
            agent_name="researcher",
            source_id="call-abc",
            timestamp=datetime.now(UTC).isoformat(),
            data={"model": "gpt-4", "tokens": 100},
            sequence=0,
        )
        assert event.layer == "llm"
        assert event.event_type == "trace_record"
        assert event.sequence == 0

    def test_valid_run_event(self):
        event = UIEvent(
            layer="run",
            event_type="tool_call",
            agent_id="agent-002",
            agent_name="coder",
            source_id="run-xyz",
            timestamp=datetime.now(UTC).isoformat(),
            data={"tool": "bash", "args": {}},
            sequence=5,
        )
        assert event.layer == "run"

    def test_valid_agent_event(self):
        event = UIEvent(
            layer="agent",
            event_type="state_change",
            agent_id="agent-003",
            agent_name="planner",
            source_id="agent-003",
            timestamp=datetime.now(UTC).isoformat(),
            data={"state": "ready"},
            sequence=1,
        )
        assert event.layer == "agent"

    def test_valid_team_event(self):
        event = UIEvent(
            layer="team",
            event_type="member_joined",
            agent_id="agent-004",
            agent_name="lead",
            source_id="team-alpha",
            timestamp=datetime.now(UTC).isoformat(),
            data={"team": "alpha"},
            sequence=0,
        )
        assert event.layer == "team"

    def test_invalid_layer_rejected(self):
        with pytest.raises(ValidationError):
            UIEvent(
                layer="invalid",
                event_type="test",
                agent_id="a",
                agent_name="b",
                source_id="c",
                timestamp=datetime.now(UTC).isoformat(),
                data={},
                sequence=0,
            )

    def test_serialization_roundtrip(self):
        event = UIEvent(
            layer="llm",
            event_type="trace_record",
            agent_id="agent-001",
            agent_name="researcher",
            source_id="call-abc",
            timestamp="2026-03-03T12:00:00+00:00",
            data={"model": "gpt-4"},
            sequence=42,
        )
        dumped = event.model_dump()
        restored = UIEvent(**dumped)
        assert restored == event

    def test_json_serialization(self):
        event = UIEvent(
            layer="llm",
            event_type="trace_record",
            agent_id="agent-001",
            agent_name="researcher",
            source_id="call-abc",
            timestamp="2026-03-03T12:00:00+00:00",
            data={"tokens": 100},
            sequence=0,
        )
        json_str = event.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["layer"] == "llm"
        assert parsed["data"]["tokens"] == 100

    def test_negative_sequence_rejected(self):
        with pytest.raises(ValidationError):
            UIEvent(
                layer="llm",
                event_type="test",
                agent_id="a",
                agent_name="b",
                source_id="c",
                timestamp=datetime.now(UTC).isoformat(),
                data={},
                sequence=-1,
            )


class TestControlMessage:
    def test_valid_cancel(self):
        msg = ControlMessage(
            action="cancel",
            target="agent-001",
            data={},
            request_id="req-001",
        )
        assert msg.action == "cancel"
        assert msg.target == "agent-001"

    def test_valid_steer(self):
        msg = ControlMessage(
            action="steer",
            target="agent-002",
            data={"instruction": "focus on security"},
            request_id="req-002",
        )
        assert msg.action == "steer"

    def test_valid_config(self):
        msg = ControlMessage(
            action="config",
            target="agent-003",
            data={"max_tokens": 2000},
            request_id="req-003",
        )
        assert msg.action == "config"

    def test_valid_ping(self):
        msg = ControlMessage(
            action="ping",
            target="agent-001",
            data={},
            request_id="req-004",
        )
        assert msg.action == "ping"

    def test_valid_shutdown(self):
        msg = ControlMessage(
            action="shutdown",
            target="agent-001",
            data={},
            request_id="req-005",
        )
        assert msg.action == "shutdown"

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            ControlMessage(
                action="destroy",
                target="agent-001",
                data={},
                request_id="req-006",
            )

    def test_serialization_roundtrip(self):
        msg = ControlMessage(
            action="cancel",
            target="agent-001",
            data={"reason": "user request"},
            request_id="req-007",
        )
        dumped = msg.model_dump()
        restored = ControlMessage(**dumped)
        assert restored == msg


class TestControlResponse:
    def test_valid_success(self):
        resp = ControlResponse(
            request_id="req-001",
            status="ok",
            data={"cancelled": True},
        )
        assert resp.status == "ok"

    def test_valid_error(self):
        resp = ControlResponse(
            request_id="req-002",
            status="error",
            data={"message": "agent busy"},
        )
        assert resp.status == "error"

    def test_serialization_roundtrip(self):
        resp = ControlResponse(
            request_id="req-001",
            status="ok",
            data={},
        )
        dumped = resp.model_dump()
        restored = ControlResponse(**dumped)
        assert restored == resp


class TestAgentRegistration:
    def test_minimal_registration(self):
        reg = AgentRegistration(
            agent_id="agent-001",
            agent_name="researcher",
            model="gpt-4",
            provider="openai",
            connected_at="2026-03-03T12:00:00+00:00",
        )
        assert reg.agent_id == "agent-001"
        assert reg.team is None
        assert reg.tools == []
        assert reg.modules == []
        assert reg.workspace is None
        assert reg.meta == {}
        assert reg.last_event_at is None
        assert reg.sequence == 0

    def test_full_registration(self):
        reg = AgentRegistration(
            agent_id="agent-002",
            agent_name="coder",
            model="claude-opus-4-6",
            provider="anthropic",
            team="alpha",
            tools=["bash", "read", "write"],
            modules=["pulse", "ui_reporter"],
            workspace="/tmp/workspace",
            meta={"version": "1.0"},
            connected_at="2026-03-03T12:00:00+00:00",
            last_event_at="2026-03-03T12:01:00+00:00",
            sequence=42,
        )
        assert reg.team == "alpha"
        assert len(reg.tools) == 3
        assert reg.sequence == 42

    def test_serialization_roundtrip(self):
        reg = AgentRegistration(
            agent_id="agent-001",
            agent_name="researcher",
            model="gpt-4",
            provider="openai",
            connected_at="2026-03-03T12:00:00+00:00",
        )
        dumped = reg.model_dump()
        restored = AgentRegistration(**dumped)
        assert restored == reg
