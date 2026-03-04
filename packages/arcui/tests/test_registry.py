"""Tests for AgentRegistry — in-memory agent tracking."""

from __future__ import annotations

from unittest.mock import MagicMock

from arcui.registry import AgentEntry, AgentRegistry
from arcui.types import AgentRegistration


def _make_registration(agent_id: str = "agent-001", **kw) -> AgentRegistration:
    defaults = {
        "agent_id": agent_id,
        "agent_name": "test-agent",
        "model": "gpt-4",
        "provider": "openai",
        "connected_at": "2026-03-03T12:00:00+00:00",
    }
    defaults.update(kw)
    return AgentRegistration(**defaults)


class TestAgentRegistryRegister:
    def test_register_and_get(self):
        registry = AgentRegistry()
        reg = _make_registration()
        ws = MagicMock()
        entry = registry.register("agent-001", ws, reg)
        assert isinstance(entry, AgentEntry)
        assert entry.registration.agent_id == "agent-001"
        assert registry.get("agent-001") is entry

    def test_register_assigns_ws(self):
        registry = AgentRegistry()
        ws = MagicMock()
        entry = registry.register("agent-001", ws, _make_registration())
        assert entry.ws is ws

    def test_list_agents(self):
        registry = AgentRegistry()
        registry.register("a1", MagicMock(), _make_registration("a1"))
        registry.register("a2", MagicMock(), _make_registration("a2"))
        agents = registry.list_agents()
        assert len(agents) == 2
        ids = {a.agent_id for a in agents}
        assert ids == {"a1", "a2"}


class TestAgentRegistryUnregister:
    def test_unregister_removes_agent(self):
        registry = AgentRegistry()
        registry.register("a1", MagicMock(), _make_registration("a1"))
        registry.unregister("a1")
        assert registry.get("a1") is None

    def test_unregister_nonexistent_no_error(self):
        registry = AgentRegistry()
        registry.unregister("nonexistent")  # Should not raise

    def test_unregister_decrements_count(self):
        registry = AgentRegistry()
        registry.register("a1", MagicMock(), _make_registration("a1"))
        registry.register("a2", MagicMock(), _make_registration("a2"))
        assert len(registry.list_agents()) == 2
        registry.unregister("a1")
        assert len(registry.list_agents()) == 1


class TestAgentRegistryCapacity:
    def test_is_full_when_at_capacity(self):
        registry = AgentRegistry(max_agents=2)
        registry.register("a1", MagicMock(), _make_registration("a1"))
        registry.register("a2", MagicMock(), _make_registration("a2"))
        assert registry.is_full() is True

    def test_not_full_under_capacity(self):
        registry = AgentRegistry(max_agents=10)
        registry.register("a1", MagicMock(), _make_registration("a1"))
        assert registry.is_full() is False

    def test_default_capacity_100(self):
        registry = AgentRegistry()
        assert registry.max_agents == 100


class TestAgentRegistryGetNone:
    def test_get_nonexistent_returns_none(self):
        registry = AgentRegistry()
        assert registry.get("nonexistent") is None
