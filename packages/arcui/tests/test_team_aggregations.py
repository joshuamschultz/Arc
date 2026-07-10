"""Tests for fleet (team-level) HTTP routes — SPEC-022 Phase 2 task 2.3.

Each endpoint aggregates per-agent data from the gateway-driven roster
and ``team/<agent>/`` filesystem reads. Routes are pure read; no writes
to ``team/`` (acceptance criterion 15) — verified end-to-end by SHA-256
snapshot tests in Phase 8.
"""

from __future__ import annotations

import json
from pathlib import Path

from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.observe import Observe
from arcui.registry import AgentRegistry
from arcui.routes.team_pages import routes as team_routes
from arcui.types import AgentRegistration


def _write_worm_audit(data_dir: Path, *, seq: int, actor_did: str) -> None:
    """Append one signed-chain record to the durable WORM file arcstore mirrors."""
    worm = data_dir / "worm"
    worm.mkdir(parents=True, exist_ok=True)
    line = {
        "seq": seq,
        "event_hash": f"hash-{seq}",
        "prev_hash": f"hash-{seq - 1}" if seq else "",
        "signature": "sig",
        "event": {
            "ts": f"2026-05-31T00:00:{seq:02d}+00:00",
            "actor_did": actor_did,
            "action": "gateway.fs.read",
            "target": "tool:x",
            "outcome": "allow",
        },
    }
    with (worm / "audit-chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _make_app(team_root: Path | None = None) -> tuple[Starlette, AuthConfig, AgentRegistry]:
    auth = AuthConfig(
        {
            "viewer_token": "viewer",
            "operator_token": "operator",
        }
    )
    registry = AgentRegistry()
    app = Starlette(routes=team_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.observe = Observe()
    app.state.team_root = team_root

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
        # Loader convention: skills are SKILL.md folders under the skills/
        # subdir of a capabilities root (workspace/capabilities/skills/<name>/).
        skill_dir = ws / "capabilities" / "skills" / f"{name}_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
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

    def test_roster_has_no_degraded_field(self, tmp_path):
        """Task #19 — the roster's ``degraded`` field was derived from
        ``~/.arcagent/agent-state.json``, a file only the non-canonical
        ``scripts/arc-stack.sh`` ever wrote. Under the embedded-gateway
        architecture nothing writes that file, so ``degraded`` was always
        stale/false for every real deployment. Removed outright rather than
        kept as a permanently-false placeholder (no fallback shim) — online
        status is already derived live from ``app.state.agent_registry``
        (see ``embedded_agents.py``), which is the only honest signal that
        exists today.
        """
        team = _build_team(tmp_path, [("alpha", "")])
        app, auth, registry = _make_app(team_root=team)
        registry.register(
            "alpha",
            AgentRegistration(
                agent_id="alpha",
                agent_name="alpha",
                model="openai/gpt-4o",
                provider="openai",
                connected_at="2026-07-10T12:00:00+00:00",
            ),
        )
        client = TestClient(app)
        resp = client.get("/api/team/roster", headers=_viewer(auth))
        agents = {a["agent_id"]: a for a in resp.json()["agents"]}
        assert "degraded" not in agents["alpha"]


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
    def test_returns_recent_events(self, tmp_path, _isolated_arc_data_dir: Path):
        # Real path: the fleet audit tab reads the durable signed chain
        # (arcstore WORM mirror) through Observe.audit — no buffer.
        from arcui.server import create_app

        for i in range(3):
            _write_worm_audit(_isolated_arc_data_dir, seq=i, actor_did=f"did:arc:a{i}")

        team = _build_team(tmp_path, [("alpha", "")])
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
            resp = client.get("/api/team/audit", headers=_viewer(auth))
        assert resp.status_code == 200
        assert len(resp.json()["events"]) == 3

    def test_limit_param(self, tmp_path, _isolated_arc_data_dir: Path):
        from arcui.server import create_app

        for i in range(50):
            _write_worm_audit(_isolated_arc_data_dir, seq=i, actor_did="did:arc:alpha")

        team = _build_team(tmp_path, [("alpha", "")])
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
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
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text("garbage", encoding="utf-8")
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
