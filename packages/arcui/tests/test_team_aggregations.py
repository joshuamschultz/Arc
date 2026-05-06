"""Tests for fleet (team-level) HTTP routes — SPEC-022 Phase 2 task 2.3.

Each endpoint aggregates per-agent data from the gateway-driven roster
and ``team/<agent>/`` filesystem reads. Routes are pure read; no writes
to ``team/`` (acceptance criterion 15) — verified end-to-end by SHA-256
snapshot tests in Phase 8.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.team_pages import routes as team_routes
from arcui.types import AgentRegistration


def _make_app(team_root: Path | None = None, agent_state_file: Path | None = None) -> tuple[Starlette, AuthConfig, AgentRegistry]:
    auth = AuthConfig(
        {
            "viewer_token": "viewer",
            "operator_token": "operator",
            "agent_token": "agent-secret",
        }
    )
    registry = AgentRegistry()
    app = Starlette(routes=team_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.audit_buffer = deque(maxlen=1000)
    app.state.team_root = team_root
    app.state.agent_state_file = agent_state_file

    def _roster_provider() -> list[team_roster.RosterEntry]:
        if app.state.team_root is None:
            return []
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=app.state.team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth, registry


def _build_team(tmp_path: Path, agents: list[tuple[str, str]]) -> Path:
    """Create ``tmp_path/team/<name>_agent`` for each (name, policy_md) pair."""
    root = tmp_path / "team"
    root.mkdir()
    for name, policy in agents:
        agent_dir = root / f"{name}_agent"
        agent_dir.mkdir()
        (agent_dir / "arcagent.toml").write_text(
            f'[agent]\nname = "{name}"\norg = "research"\n'
            f'[identity]\ndid = "did:arc:{name}"\n'
            '[llm]\nmodel = "openai/gpt-4o"\n',
            encoding="utf-8",
        )
        ws = agent_dir / "workspace"
        ws.mkdir()
        if policy:
            (ws / "policy.md").write_text(policy, encoding="utf-8")
        # tasks.json + skills/ for those endpoints
        (ws / "tasks.json").write_text(
            f'[{{"id": "{name}-t1", "title": "{name} task", "status": "open"}}]',
            encoding="utf-8",
        )
        skills = ws / "skills"
        skills.mkdir()
        (skills / f"{name}_skill.md").write_text(
            f"---\nname: {name}_skill\ndescription: skill of {name}\n---\nbody\n",
            encoding="utf-8",
        )
    return root


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


# ---------------------------------------------------------------------------
# /api/team/roster
# ---------------------------------------------------------------------------


class TestRoster:
    def test_empty_when_no_team_root(self):
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    def test_lists_agents(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        assert resp.status_code == 200
        ids = {a["agent_id"] for a in resp.json()["agents"]}
        assert ids == {"alpha", "beta"}

    def test_overlays_online_status(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, registry = _make_app(team_root=team)
        registry.register(
            "alpha",
            MagicMock(),
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                connected_at="2026-04-29T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["online"] is True
        assert agents["beta"]["online"] is False


# ---------------------------------------------------------------------------
# /api/team/policy/{bullets,stats}
# ---------------------------------------------------------------------------


class TestFleetPolicy:
    def test_bullets_aggregated_with_agent_id(self, tmp_path):
        policy_alpha = (
            "- [P01] Be helpful {score:8, uses:5, reviewed:2026-04-01, "
            "created:2026-01-01, source:s1}\n"
        )
        policy_beta = (
            "- [P01] Be careful {score:6, uses:2, reviewed:2026-04-15, "
            "created:2026-02-01, source:s2}\n"
        )
        team = _build_team(tmp_path, [("alpha", policy_alpha), ("beta", policy_beta)])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/policy/bullets", headers=_viewer(auth))
        assert resp.status_code == 200
        bullets = resp.json()["bullets"]
        # Each bullet stamped with agent_id so the UI can badge it
        agents_with_p01 = {b["agent_id"] for b in bullets if b["id"] == "P01"}
        assert agents_with_p01 == {"alpha", "beta"}

    def test_stats_aggregates_across_fleet(self, tmp_path):
        policy_alpha = (
            "- [P01] A {score:8, uses:1, reviewed:2026-04-01, created:2026-01-01, source:s1}\n"
            "- [P02] B {score:1, uses:1, reviewed:2026-04-01, created:2026-01-01, source:s1}\n"
        )
        policy_beta = (
            "- [P03] C {score:6, uses:1, reviewed:2026-04-01, created:2026-01-01, source:s2}\n"
        )
        team = _build_team(tmp_path, [("alpha", policy_alpha), ("beta", policy_beta)])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/policy/stats", headers=_viewer(auth))
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total"] == 3
        assert stats["active"] == 2
        assert stats["retired"] == 1
        # avg over active = (8+6)/2 = 7
        assert abs(stats["avg_score"] - 7.0) < 0.01
        per_agent = {a["agent_id"]: a for a in stats["per_agent"]}
        assert per_agent["alpha"]["total"] == 2
        assert per_agent["beta"]["total"] == 1


# ---------------------------------------------------------------------------
# /api/team/tasks
# ---------------------------------------------------------------------------


class TestFleetTasks:
    def test_aggregates_tasks_with_agent_id(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        ids = {t["id"] for t in tasks}
        assert ids == {"alpha-t1", "beta-t1"}
        for t in tasks:
            assert "agent_id" in t


# ---------------------------------------------------------------------------
# /api/team/tools-skills
# ---------------------------------------------------------------------------


class TestFleetToolsSkills:
    def test_skills_directory(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        assert "skills" in body
        assert "tools" in body
        skill_names = {s["name"] for s in body["skills"]}
        assert "alpha_skill" in skill_names
        assert "beta_skill" in skill_names

    def test_tools_matrix_uses_live_registrations(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, registry = _make_app(team_root=team)
        registry.register(
            "alpha",
            MagicMock(),
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                tools=["fs.read", "search"],
                connected_at="2026-04-29T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        tools = resp.json()["tools"]
        names = {t["name"] for t in tools}
        assert "fs.read" in names
        assert "search" in names


# ---------------------------------------------------------------------------
# /api/team/audit
# ---------------------------------------------------------------------------


class TestFleetAudit:
    def test_returns_recent_events(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, _ = _make_app(team_root=team)
        for i in range(3):
            app.state.audit_buffer.append(
                {
                    "agent_id": "alpha",
                    "action": "gateway.fs.read",
                    "outcome": "allow",
                    "seq": i,
                }
            )
        client = TestClient(app)
        resp = client.get("/api/team/audit", headers=_viewer(auth))
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 3

    def test_limit_param(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, _ = _make_app(team_root=team)
        for i in range(50):
            app.state.audit_buffer.append({"agent_id": "alpha", "seq": i})
        client = TestClient(app)
        resp = client.get("/api/team/audit?limit=10", headers=_viewer(auth))
        assert len(resp.json()["events"]) == 10


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_roster_requires_token(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, _, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/roster")
        assert resp.status_code == 401


# Coverage — error / edge branches


class TestEdgeCases:
    def test_audit_invalid_limit(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/audit?limit=abc", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_tasks_skips_malformed_json(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        # Corrupt one tasks.json — fleet endpoint should still answer with what it can.
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text(
            "garbage", encoding="utf-8"
        )
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"tasks": []}

    def test_tasks_object_root_returns_empty(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text(
            '{"single": "object"}', encoding="utf-8"
        )
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tasks", headers=_viewer(auth))
        assert resp.json() == {"tasks": []}

    def test_no_skills_dir_for_one_agent(self, tmp_path):
        team = _build_team(tmp_path, [("alpha", "")])
        # alpha has skills/. add a beta agent without skills/
        beta = team / "beta_agent"
        beta.mkdir()
        (beta / "arcagent.toml").write_text(
            '[agent]\nname = "beta"\n[identity]\ndid = "did:arc:beta"\n',
            encoding="utf-8",
        )
        (beta / "workspace").mkdir()
        app, auth, _ = _make_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/team/tools-skills", headers=_viewer(auth))
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()["skills"]}
        assert "alpha_skill" in names
        # beta has no skills, but its absence is silent — no crash.


# ---------------------------------------------------------------------------
# SPEC-025 Track C4 — degraded field in roster (per-agent connect state)
# ---------------------------------------------------------------------------


class TestRosterDegradedState:
    """Three states the roster endpoint must handle for each agent:

    1. Agent is online (connected) — degraded=False.
    2. Agent is in arc-stack's state file as "degraded" — degraded=True,
       online=False.  This is the demo-failure scenario: agent dir exists
       on disk, arc-stack attempted to start it, but it crashed at startup.
    3. Agent has no entry in the state file — degraded=False (we have no
       data; treat as unknown / not-attempted-on-this-host).
    """

    def test_agent_online_is_not_degraded(self, tmp_path: Path) -> None:
        """Happy path: connected agent → degraded=False."""
        team = _build_team(tmp_path, [("alpha", "")])
        state_file = tmp_path / "agent-state.json"
        state_file.write_text('{"alpha": "connected"}', encoding="utf-8")
        app, auth, registry = _make_app(team_root=team, agent_state_file=state_file)
        registry.register(
            "alpha",
            MagicMock(),
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                connected_at="2026-05-05T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["online"] is True
        assert agents["alpha"]["degraded"] is False

    def test_agent_in_manifest_but_not_connected_is_degraded(self, tmp_path: Path) -> None:
        """Failure scenario: arc-stack attempted the agent but it crashed."""
        team = _build_team(tmp_path, [("alpha", ""), ("beta", "")])
        state_file = tmp_path / "agent-state.json"
        # alpha connected; beta crashed during startup
        state_file.write_text('{"alpha": "connected", "beta": "degraded"}', encoding="utf-8")
        app, auth, registry = _make_app(team_root=team, agent_state_file=state_file)
        registry.register(
            "alpha",
            MagicMock(),
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                connected_at="2026-05-05T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["degraded"] is False
        assert agents["beta"]["online"] is False
        assert agents["beta"]["degraded"] is True

    def test_agent_not_in_state_file_is_not_degraded(self, tmp_path: Path) -> None:
        """Agent exists on disk but was never recorded in the state file."""
        team = _build_team(tmp_path, [("alpha", "")])
        state_file = tmp_path / "agent-state.json"
        # state file exists but alpha has no entry
        state_file.write_text("{}", encoding="utf-8")
        app, auth, _ = _make_app(team_root=team, agent_state_file=state_file)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["degraded"] is False
        # online stays whatever the registry says (not connected here)
        assert agents["alpha"]["online"] is False

    def test_missing_state_file_does_not_break_roster(self, tmp_path: Path) -> None:
        """If agent-state.json doesn't exist, roster still returns normally."""
        team = _build_team(tmp_path, [("alpha", "")])
        non_existent = tmp_path / "no-such-file.json"
        app, auth, _ = _make_app(team_root=team, agent_state_file=non_existent)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        assert resp.status_code == 200
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["degraded"] is False

    def test_unknown_agent_id_in_state_file_is_dropped(self, tmp_path: Path) -> None:
        """SPEC-025 §M2 — state-file entries for unknown agent_ids are ignored.

        Defense-in-depth: arcui must not surface arbitrary agent_id strings
        that an out-of-band writer placed in the state file.
        """
        team = _build_team(tmp_path, [("alpha", "")])
        state_file = tmp_path / "agent-state.json"
        # alpha is real; "ghost" is fabricated; "../../../etc" is malicious.
        state_file.write_text(
            '{"alpha": "degraded", "ghost": "degraded", "../../../etc": "degraded"}',
            encoding="utf-8",
        )
        app, auth, _ = _make_app(team_root=team, agent_state_file=state_file)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        # alpha is in the live roster — degraded honoured.
        assert agents["alpha"]["degraded"] is True
        # ghost and the path-traversal entry are NOT in the response at all.
        assert "ghost" not in agents
        assert "../../../etc" not in agents


    def test_stale_state_file_is_ignored(self, tmp_path: Path) -> None:
        """SPEC-025 §TD-5 — state files older than max-age fall through to defaults.

        Models the arc-stack-restart race: arcui must not surface degraded=True
        based on a state file that belongs to a previous arc-stack lifetime.
        """
        team = _build_team(tmp_path, [("alpha", "")])
        state_file = tmp_path / "agent-state.json"
        # Written 2 hours ago — far past the 10-minute freshness window.
        stale_iso = "2020-01-01T00:00:00Z"
        state_file.write_text(
            f'{{"_meta": {{"written_at": "{stale_iso}"}}, "alpha": "degraded"}}',
            encoding="utf-8",
        )
        app, auth, _ = _make_app(team_root=team, agent_state_file=state_file)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        # alpha is NOT degraded — stale state was ignored.
        assert agents["alpha"]["degraded"] is False

    def test_legacy_state_without_meta_is_accepted(self, tmp_path: Path) -> None:
        """Backward-compat: state files written by older arc-stack (no _meta) still apply."""
        team = _build_team(tmp_path, [("alpha", "")])
        state_file = tmp_path / "agent-state.json"
        # No _meta field at all.
        state_file.write_text('{"alpha": "degraded"}', encoding="utf-8")
        app, auth, _ = _make_app(team_root=team, agent_state_file=state_file)
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert agents["alpha"]["degraded"] is True
