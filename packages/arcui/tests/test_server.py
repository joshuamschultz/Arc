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


# --- SPEC-022 Phase 2: arcgateway integration -------------------------------


def test_app_state_team_root_default_none():
    """team_root defaults to None when not provided."""
    app = create_app()
    assert hasattr(app.state, "team_root")
    assert app.state.team_root is None


def test_app_state_team_root_passed_through(tmp_path):
    """create_app(team_root=...) stores the path on app.state."""
    app = create_app(team_root=tmp_path)
    assert app.state.team_root == tmp_path


def test_app_state_roster_provider_returns_empty_when_no_team_root():
    """roster_provider returns [] when team_root is None — keeps routes pure."""
    app = create_app()
    assert callable(app.state.roster_provider)
    assert app.state.roster_provider() == []


def test_app_state_roster_provider_overlays_online_status(tmp_path):
    """roster_provider walks team_root and overlays online flag from registry."""
    from unittest.mock import MagicMock

    from arcui.types import AgentRegistration

    # Synthetic team dir with two agents
    a1 = tmp_path / "alpha_agent"
    a1.mkdir()
    (a1 / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\ntype = "research"\n'
        '[identity]\ndid = "did:arc:alpha"\n'
        '[llm]\nmodel = "openai/gpt-4o"\n',
        encoding="utf-8",
    )
    a2 = tmp_path / "beta_agent"
    a2.mkdir()
    (a2 / "arcagent.toml").write_text(
        '[agent]\nname = "beta"\n'
        '[identity]\ndid = "did:arc:beta"\n',
        encoding="utf-8",
    )

    app = create_app(team_root=tmp_path)

    # Register only alpha as online
    reg = AgentRegistration(
        agent_id="alpha",
        agent_name="alpha",
        model="openai/gpt-4o",
        provider="openai",
        connected_at="2026-04-29T12:00:00+00:00",
    )
    app.state.agent_registry.register("alpha", MagicMock(), reg)

    roster = app.state.roster_provider()
    by_id = {r.agent_id: r for r in roster}
    assert by_id["alpha"].online is True
    assert by_id["beta"].online is False


def test_roster_provider_overridable_by_tests():
    """app.state.roster_provider can be replaced by tests for in-memory fixtures."""
    app = create_app()

    sentinel = object()

    def stub() -> list:
        return [sentinel]

    app.state.roster_provider = stub
    assert app.state.roster_provider() == [sentinel]
