"""Tests for the AgentRegistration model."""

from __future__ import annotations

from arcui.types import AgentRegistration


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
