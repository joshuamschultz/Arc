"""Tests for server.py — verify new multi-agent components are wired."""

from __future__ import annotations

from arcui.server import create_app


def test_app_state_has_agent_registry():
    """create_app() should store an AgentRegistry on app.state."""
    app = create_app()
    assert hasattr(app.state, "agent_registry")
    from arcui.registry import AgentRegistry

    assert isinstance(app.state.agent_registry, AgentRegistry)


def test_app_state_has_subscription_manager():
    """create_app() should store a SubscriptionManager on app.state."""
    app = create_app()
    assert hasattr(app.state, "subscription_manager")
    from arcui.subscription import SubscriptionManager

    assert isinstance(app.state.subscription_manager, SubscriptionManager)


def test_app_state_has_pending_controls():
    """create_app() should store a pending_controls dict on app.state."""
    app = create_app()
    assert hasattr(app.state, "pending_controls")
    assert isinstance(app.state.pending_controls, dict)


def test_event_buffer_has_subscription_manager():
    """EventBuffer should be wired with the SubscriptionManager."""
    app = create_app()
    event_buffer = app.state.event_buffer
    assert event_buffer._sub_mgr is app.state.subscription_manager


def test_max_agents_parameter():
    """create_app(max_agents=50) should configure registry capacity."""
    app = create_app(max_agents=50)
    registry = app.state.agent_registry
    assert registry.max_agents == 50


def test_agent_routes_registered():
    """Agent WS and REST routes should be present in the app."""
    app = create_app()
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/agent/connect" in paths
    assert "/api/agents" in paths
    assert "/api/agents/{id}" in paths
    assert "/api/agents/{id}/control" in paths
