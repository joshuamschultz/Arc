"""Tests for server.py — verify new multi-agent components are wired."""

from __future__ import annotations

from arcstore.backends.memory import FakeBackend
from starlette.testclient import TestClient

from arcui.server import create_app


def _app(**kwargs):
    return create_app(arcstore_backend=FakeBackend(), **kwargs)


def test_app_state_has_agent_registry():
    """create_app() should store an AgentRegistry on app.state."""
    app = _app()
    assert hasattr(app.state, "agent_registry")
    from arcui.registry import AgentRegistry

    assert isinstance(app.state.agent_registry, AgentRegistry)


def test_app_state_has_observe():
    """create_app() should store an Observe instance on app.state (SPEC-026 FR-5)."""
    app = _app()
    assert hasattr(app.state, "observe")
    from arcui.observe import Observe

    assert isinstance(app.state.observe, Observe)


def test_app_state_has_circuit_breakers_list():
    """create_app() should store an empty circuit_breakers list on app.state."""
    app = _app()
    assert hasattr(app.state, "circuit_breakers")
    assert isinstance(app.state.circuit_breakers, list)


def test_max_agents_parameter():
    """create_app(max_agents=50) should configure registry capacity."""
    app = _app(max_agents=50)
    registry = app.state.agent_registry
    assert registry.max_agents == 50


def test_agent_routes_registered():
    """Agent REST routes should be present in the app (SPEC-026 FR-5: /api/agent/connect WS deleted)."""
    app = _app()
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/agents" in paths
    assert "/api/agents/{id}" in paths


# --- SPEC-022 Phase 2: arcgateway integration -------------------------------


def test_app_state_team_root_default_none():
    """team_root defaults to None when not provided."""
    app = _app()
    assert hasattr(app.state, "team_root")
    assert app.state.team_root is None


def test_app_state_team_root_passed_through(tmp_path):
    """create_app(team_root=...) stores the path on app.state."""
    app = _app(team_root=tmp_path)
    assert app.state.team_root == tmp_path


def test_app_state_roster_provider_returns_empty_when_no_team_root():
    """roster_provider returns [] when team_root is None — keeps routes pure."""
    app = _app()
    assert callable(app.state.roster_provider)
    assert app.state.roster_provider() == []


def test_app_state_roster_provider_overlays_online_status(tmp_path):
    """roster_provider walks team_root and overlays online flag from registry."""
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
        '[agent]\nname = "beta"\n[identity]\ndid = "did:arc:beta"\n',
        encoding="utf-8",
    )

    app = _app(team_root=tmp_path)

    # Register only alpha as online
    reg = AgentRegistration(
        agent_id="alpha",
        agent_name="alpha",
        model="openai/gpt-4o",
        provider="openai",
        connected_at="2026-04-29T12:00:00+00:00",
    )
    app.state.agent_registry.register("alpha", reg)

    roster = app.state.roster_provider()
    by_id = {r.agent_id: r for r in roster}
    assert by_id["alpha"].online is True
    assert by_id["beta"].online is False


def test_roster_provider_overridable_by_tests():
    """app.state.roster_provider can be replaced by tests for in-memory fixtures."""
    app = _app()

    sentinel = object()

    def stub() -> list:
        return [sentinel]

    app.state.roster_provider = stub
    assert app.state.roster_provider() == [sentinel]


# ---------------------------------------------------------------------------
# Deep links — every path the browser router owns must also load directly
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    from arcui.auth import AuthConfig

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = _app(auth_config=auth)
    app.state.auth_config = auth
    return TestClient(app)


def test_refreshing_a_router_path_serves_the_dashboard() -> None:
    """Opening or refreshing /connections must work, not 404.

    The routes live in the React router, so the server has never had one for
    ``/connections``. Reaching it by clicking worked; refreshing it, opening it
    in a new tab, or sharing the URL returned "Not Found" — which reads as the
    feature being broken rather than as a routing gap.
    """
    resp = _client().get("/connections")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_a_nested_router_path_serves_the_dashboard() -> None:
    """The same has to hold for a path with segments, e.g. an agent's own page."""
    resp = _client().get("/agents/coder_agent/connections")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_an_unknown_api_path_is_still_a_404() -> None:
    """A mistyped API route must stay a 404 an integrator can see.

    Serving the dashboard shell here would answer 200 with HTML that parses as
    success, turning a typo into a silent, confusing failure.
    """
    resp = _client().get("/api/nope", headers={"Authorization": "Bearer operator"})

    assert resp.status_code == 404
    assert "text/html" not in resp.headers.get("content-type", "")
