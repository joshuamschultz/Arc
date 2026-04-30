"""SPEC-022 Acceptance Criterion 15 — arcui never writes under team/.

Snapshot SHA-256 + mtime + size of every file under a synthetic ``team/``
tree, exercise every read endpoint that arcui exposes per agent + per fleet,
then re-snapshot. The two snapshots MUST be byte-identical.

This is the structural proof for the hard architectural invariant:
    "arcui is an observer. No code, state, or scratch files written into
     team/ or team/<agent>/."

The test exercises:
  - GET /api/team/roster
  - GET /api/team/policy/{bullets,stats}
  - GET /api/team/tasks
  - GET /api/team/tools-skills
  - GET /api/team/audit
  - GET /api/agents/{id}
  - GET /api/agents/{id}/config
  - GET /api/agents/{id}/files/tree?root=workspace|agent
  - GET /api/agents/{id}/files/read?path=...
  - GET /api/agents/{id}/skills
  - GET /api/agents/{id}/tools
  - GET /api/agents/{id}/sessions
  - GET /api/agents/{id}/sessions/{sid}
  - GET /api/agents/{id}/stats
  - GET /api/agents/{id}/traces
  - GET /api/agents/{id}/audit
  - GET /api/agents/{id}/policy{,/bullets,/stats}
  - GET /api/agents/{id}/tasks
  - GET /api/agents/{id}/schedules
"""

from __future__ import annotations

import hashlib
from collections import deque
from pathlib import Path

from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes
from arcui.routes.team_pages import routes as team_routes


def _build_team_dir(tmp_path: Path) -> Path:
    """Reuse the same realistic layout as test_agents_routes._build_team_dir."""
    root = tmp_path / "team"
    root.mkdir()
    agent = root / "alpha_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\norg = "r"\ntype = "scout"\n'
        '[identity]\ndid = "did:arc:alpha"\n'
        '[llm]\nmodel = "openai/gpt-4o"\nmax_tokens = 4096\n'
        '[ui]\ndisplay_name = "Alpha"\ncolor = "#abcdef"\n',
        encoding="utf-8",
    )
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "policy.md").write_text(
        "- [P01] Be helpful {score:8, uses:5, reviewed:2026-04-01, "
        "created:2026-03-01, source:s-1}\n",
        encoding="utf-8",
    )
    (ws / "identity.md").write_text("# I am alpha\n", encoding="utf-8")
    (ws / "pulse.md").write_text("# Pulse\n", encoding="utf-8")
    (ws / "tasks.json").write_text('[{"id":"t1","subject":"x","status":"open"}]', encoding="utf-8")
    (ws / "schedules.json").write_text('[{"id":"s1","cron":"* * * * *"}]', encoding="utf-8")
    (ws / "skills").mkdir()
    (ws / "skills" / "demo.md").write_text(
        "---\nname: demo\ndescription: x\n---\nbody\n", encoding="utf-8"
    )
    (ws / "sessions").mkdir()
    (ws / "sessions" / "s-1.jsonl").write_text(
        '{"role":"user","content":"hi"}\n', encoding="utf-8"
    )
    return root


def _snapshot(team_root: Path) -> dict[str, tuple[int, int, str]]:
    """Map of relative-path → (size, mtime_ns, sha256)."""
    snap: dict[str, tuple[int, int, str]] = {}
    for p in sorted(team_root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(team_root))
        st = p.stat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        snap[rel] = (st.st_size, st.st_mtime_ns, digest)
    return snap


def _build_app(team_root: Path) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    registry = AgentRegistry()
    app = Starlette(routes=[*agent_routes, *agent_detail_routes, *team_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.pending_controls = {}
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.audit_buffer = deque(maxlen=1000)
    app.state.team_root = team_root
    app.state.trace_store = None  # /traces gracefully returns [] when None

    def _roster_provider() -> list[team_roster.RosterEntry]:
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


class TestNoTeamWrites:
    def test_exercising_all_read_endpoints_does_not_modify_team(
        self, tmp_path: Path
    ) -> None:
        team_root = _build_team_dir(tmp_path)
        before = _snapshot(team_root)
        app, auth = _build_app(team_root)
        client = TestClient(app)
        h = _viewer(auth)

        # Fleet endpoints
        for path in (
            "/api/team/roster",
            "/api/team/policy/bullets",
            "/api/team/policy/stats",
            "/api/team/tasks",
            "/api/team/tools-skills",
            "/api/team/audit",
        ):
            resp = client.get(path, headers=h)
            assert resp.status_code == 200, (path, resp.text)

        # Per-agent endpoints
        for path in (
            "/api/agents",
            "/api/agents/alpha",
            "/api/agents/alpha/config",
            "/api/agents/alpha/files/tree?root=workspace",
            "/api/agents/alpha/files/tree?root=agent",
            "/api/agents/alpha/files/read?root=workspace&path=policy.md",
            "/api/agents/alpha/files/read?root=workspace&path=identity.md",
            "/api/agents/alpha/files/read?root=workspace&path=pulse.md",
            "/api/agents/alpha/files/read?root=workspace&path=skills/demo.md",
            "/api/agents/alpha/files/read?root=workspace&path=sessions/s-1.jsonl",
            "/api/agents/alpha/files/read?root=agent&path=arcagent.toml",
            "/api/agents/alpha/skills",
            "/api/agents/alpha/tools",
            "/api/agents/alpha/sessions",
            "/api/agents/alpha/sessions/s-1.jsonl",
            "/api/agents/alpha/stats",
            "/api/agents/alpha/traces",
            "/api/agents/alpha/audit",
            "/api/agents/alpha/policy",
            "/api/agents/alpha/policy/bullets",
            "/api/agents/alpha/policy/stats",
            "/api/agents/alpha/tasks",
            "/api/agents/alpha/schedules",
        ):
            resp = client.get(path, headers=h)
            # Some endpoints (traces, stats, tools) may legitimately be 200
            # with empty payloads or 404 when no live registration exists —
            # the only thing this test cares about is that no read mutates.
            assert resp.status_code in (200, 404), (path, resp.status_code, resp.text)

        after = _snapshot(team_root)

        # Diff for human-readable failure if anything moved.
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        modified = [k for k in before if k in after and before[k] != after[k]]
        assert not added, f"unexpected files created under team/: {added}"
        assert not removed, f"files were removed from team/: {removed}"
        assert not modified, f"files were mutated under team/: {modified}"
        assert before == after, "team/ snapshot diverged after read-only operations"
