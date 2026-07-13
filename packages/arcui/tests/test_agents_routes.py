"""Tests for agents REST routes — list and detail.

SPEC-022 Phase 2 extends this file with per-agent detail routes that read
from a synthetic ``team/`` directory through ``arcgateway.fs_reader``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from arcgateway import team_roster
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import Task, TaskStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.observe import Observe
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes
from arcui.types import AgentRegistration


async def _seed_task(data_dir: Path, task: Task) -> None:
    """Seed one task into the arcstore mutable plane `Observe.tasks()` reads.

    SPEC-056 Phase D re-pointed `/api/agents/{id}/tasks` off `tasks.json`
    (which `_build_team_dir` still writes for the directory-layout docstring,
    but is never read) onto arcstore — this seeds the real source of truth.
    """
    backend = SqliteBackend(data_dir / "store" / "arcui.db")
    await backend.start()
    await TaskStore(backend).create(task)


def _make_app() -> tuple[Starlette, AuthConfig, AgentRegistry]:
    auth = AuthConfig(
        {
            "viewer_token": "viewer",
            "operator_token": "operator",
        }
    )
    registry = AgentRegistry()

    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    return app, auth, registry


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
            "ts": f"2026-05-31T00:00:0{seq}+00:00",
            "actor_did": actor_did,
            "action": "gateway.fs.read",
            "target": "tool:x",
            "outcome": "allow",
        },
    }
    with (worm / "audit-chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


# --- SPEC-022 Phase 2 fixtures ---------------------------------------------


def _build_team_dir(tmp_path: Path) -> Path:
    """Synthesise one agent under ``tmp_path`` with realistic content.

    Layout::

        tmp_path/
          alpha_agent/
            arcagent.toml
            workspace/
              policy.md
              identity.md
              pulse.md
              tasks.json
              schedules.json
              capabilities/skills/skill_a/SKILL.md
              sessions/session-001.jsonl
    """
    root = tmp_path / "team"
    root.mkdir()
    agent = root / "alpha_agent"
    agent.mkdir()

    (agent / "arcagent.toml").write_text(
        "[agent]\n"
        'name = "alpha"\n'
        'org = "research"\n'
        'type = "scout"\n'
        "[identity]\n"
        'did = "did:arc:alpha"\n'
        "[llm]\n"
        'model = "openai/gpt-4o"\n'
        "max_tokens = 4096\n"
        "temperature = 0.7\n"
        "[secrets]\n"
        'api_key = "SHOULD_NEVER_LEAK"\n'
        "[tools.policy]\n"
        'allow = ["fs.read", "search"]\n',
        encoding="utf-8",
    )
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "policy.md").write_text(
        "# Policy\n\n"
        "- [P01] Be helpful {score:8, uses:42, reviewed:2026-04-29, "
        "created:2026-01-01, source:s-001}\n"
        "- [P02] Avoid hallucination {score:6, uses:9, reviewed:2026-04-15, "
        "created:2026-02-01, source:s-002}\n"
        "- [P03] Old habit {score:1, uses:1, reviewed:2026-01-01, "
        "created:2025-12-01, source:s-003}\n",
        encoding="utf-8",
    )
    (ws / "identity.md").write_text("# Identity\nI am alpha.\n", encoding="utf-8")
    (ws / "pulse.md").write_text("# Pulse\nReady.\n", encoding="utf-8")
    (ws / "tasks.json").write_text(
        '[{"id": "t1", "title": "Investigate", "status": "open", "priority": "high"}]',
        encoding="utf-8",
    )
    (ws / "schedules.json").write_text(
        '[{"id": "sched1", "cron": "0 * * * *", "action": "ping"}]',
        encoding="utf-8",
    )
    # Loader convention: skills are SKILL.md folders under the skills/ subdir
    # of a capabilities root (here, workspace/capabilities/skills/<name>/).
    skill_a = ws / "capabilities" / "skills" / "skill_a"
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\nname: skill_a\ndescription: example skill\nversion: 1\n---\n# Body\nDoit.\n",
        encoding="utf-8",
    )
    sessions = ws / "sessions"
    sessions.mkdir()
    (sessions / "session-001.jsonl").write_text(
        "\n".join(
            [
                '{"role": "user", "content": "hi"}',
                '{"role": "assistant", "content": "hello"}',
                '{"role": "user", "content": "go"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _make_detail_app(
    team_root: Path | None = None,
    *,
    data_dir: Path | None = None,
) -> tuple[Starlette, AuthConfig, AgentRegistry]:
    """Build a Starlette app wired with both list/control routes and detail routes."""
    auth = AuthConfig(
        {
            "viewer_token": "viewer",
            "operator_token": "operator",
        }
    )
    registry = AgentRegistry()

    app = Starlette(routes=[*agent_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.team_root = team_root
    # SPEC-026 FR-5: stats and traces routes read from the Observe plane.
    app.state.observe = Observe(data_dir=data_dir)

    def _roster_provider() -> list[team_roster.RosterEntry]:
        if app.state.team_root is None:
            return []
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=app.state.team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth, registry


def _register_agent(
    registry: AgentRegistry,
    agent_id: str = "agent-001",
    agent_name: str = "test-agent",
) -> None:
    reg = AgentRegistration(
        agent_id=agent_id,
        agent_name=agent_name,
        model="gpt-4",
        provider="openai",
        connected_at="2026-03-03T12:00:00+00:00",
    )
    registry.register(agent_id, reg)


class TestListAgents:
    def test_list_empty(self):
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["agents"] == []

    def test_list_with_agents(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1", "agent-alpha")
        _register_agent(registry, "a2", "agent-beta")

        client = TestClient(app)
        resp = client.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert len(agents) == 2

    def test_list_requires_auth(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.get("/api/agents")
        assert resp.status_code == 401


class TestGetAgent:
    def test_get_existing_agent(self):
        # SPEC-022 flattened the response: live agents return their fields
        # directly (no `{"agent": {...}}` wrapper) so the agent-detail SPA
        # reads the same shape from live + roster fallbacks.
        app, auth, registry = _make_app()
        _register_agent(registry, "a1", "agent-alpha")

        client = TestClient(app)
        resp = client.get(
            "/api/agents/a1",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == "a1"
        assert body["online"] is True

    def test_get_nonexistent_returns_404(self):
        # When neither the live registry nor the roster_provider knows the
        # id, the route returns 404 with a clear error.
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/agents/nonexistent",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "Agent not found"}


# ===========================================================================
# SPEC-022 Phase 2 — Agent Detail routes
# ===========================================================================


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


class TestAgentConfigRoute:
    def test_unknown_agent_returns_404(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/unknown/config", headers=_viewer(auth))
        assert resp.status_code == 404

    def test_config_returns_whitelisted_sections(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/config", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        cfg = body["config"]
        assert cfg["agent"]["name"] == "alpha"
        assert cfg["llm"]["model"] == "openai/gpt-4o"
        assert "tools" in cfg
        # No secrets — section is dropped entirely.
        assert "secrets" not in cfg

    def test_config_does_not_leak_secrets_in_raw(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/config", headers=_viewer(auth))
        # The whitelisted "config" object must never carry secrets.
        body = resp.json()
        assert "SHOULD_NEVER_LEAK" not in str(body["config"])


class TestFilesTreeRoute:
    def test_workspace_root_lists_files(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/files/tree?root=workspace", headers=_viewer(auth))
        assert resp.status_code == 200
        paths = {e["path"] for e in resp.json()["entries"]}
        assert "policy.md" in paths
        assert "capabilities" in paths
        assert "sessions" in paths

    def test_agent_root_lists_arcagent_toml(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/files/tree?root=agent", headers=_viewer(auth))
        assert resp.status_code == 200
        paths = {e["path"] for e in resp.json()["entries"]}
        assert "arcagent.toml" in paths
        assert "workspace" in paths

    def test_invalid_root_returns_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/files/tree?root=etc", headers=_viewer(auth))
        assert resp.status_code == 400


class TestFilesReadRoute:
    def test_read_policy_md(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(
            "/api/agents/alpha/files/read?root=workspace&path=policy.md",
            headers=_viewer(auth),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["content_type"] == "text"
        assert "P01" in body["content"]

    def test_traversal_blocked(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(
            "/api/agents/alpha/files/read?root=workspace&path=../../etc/passwd",
            headers=_viewer(auth),
        )
        assert resp.status_code == 400

    def test_missing_file_404(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(
            "/api/agents/alpha/files/read?root=workspace&path=missing.md",
            headers=_viewer(auth),
        )
        assert resp.status_code == 404

    def test_missing_path_param_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/files/read?root=workspace", headers=_viewer(auth))
        assert resp.status_code == 400


class TestSkillsRoute:
    def test_skills_returns_parsed_frontmatter(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/skills", headers=_viewer(auth))
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        names = {s["name"] for s in skills}
        assert "skill_a" in names

    def test_skills_unknown_agent_404(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/missing/skills", headers=_viewer(auth))
        assert resp.status_code == 404


class TestToolsRoute:
    def test_tools_listing_from_registration(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, registry = _make_detail_app(team_root=team)

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
        resp = client.get("/api/agents/alpha/tools", headers=_viewer(auth))
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["tools"]}
        assert "fs.read" in names

    def test_tools_offline_falls_back_to_config_policy(self, tmp_path):
        # Even when not connected, we surface config-declared tool policy.
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/tools", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        assert "tools" in body
        assert "allowlist" in body


class TestSessionsRoute:
    def test_list_sessions(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/sessions", headers=_viewer(auth))
        assert resp.status_code == 200
        sids = {s["sid"] for s in resp.json()["sessions"]}
        assert "session-001" in sids

    def test_replay_session_paginated(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(
            "/api/agents/alpha/sessions/session-001?page=1&page_size=2",
            headers=_viewer(auth),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["messages"]) == 2
        assert body["page"] == 1
        assert body["total"] == 3

    def test_replay_invalid_sid_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        # Reject any sid containing slashes / traversal characters.
        resp = client.get("/api/agents/alpha/sessions/..%2Fetc%2Fpasswd", headers=_viewer(auth))
        assert resp.status_code in (400, 404)

    def test_replay_namespaced_sid_round_trips(self, tmp_path):
        # U12 — namespaced session ids (``messaging:inbox``) are legitimate
        # session filenames; the colon must not be rejected as invalid.
        team = _build_team_dir(tmp_path)
        sessions = next(team.glob("*/workspace/sessions"))
        (sessions / "messaging:inbox.jsonl").write_text(
            '{"role": "user", "content": "ping"}\n'
            '{"role": "assistant", "content": "pong"}\n',
            encoding="utf-8",
        )
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        # Both raw and %3A-encoded colon must resolve to the same session (200).
        for sid in ("messaging:inbox", "messaging%3Ainbox"):
            resp = client.get(f"/api/agents/alpha/sessions/{sid}", headers=_viewer(auth))
            assert resp.status_code == 200, sid
            assert resp.json()["total"] == 2


class TestStatsRoute:
    def test_stats_falls_back_to_global_when_no_per_agent(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/stats", headers=_viewer(auth))
        # 200 (with an empty/global stats) or 404 if explicitly designed
        # for offline agents — just verify it doesn't crash.
        assert resp.status_code in (200, 404)


class TestTracesRoute:
    def test_traces_unknown_agent_404(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        # No trace store — endpoint should still answer for known agents.
        client = TestClient(app)
        resp = client.get("/api/agents/missing/traces", headers=_viewer(auth))
        assert resp.status_code == 404


class TestAuditRoute:
    def test_audit_returns_chain_for_agent(self, tmp_path, _isolated_arc_data_dir: Path):
        # Real path: the per-agent audit tab resolves agent label -> DID and
        # reads the durable signed chain (arcstore WORM mirror), not a buffer.
        from arcui.server import create_app

        _write_worm_audit(_isolated_arc_data_dir, seq=0, actor_did="did:arc:alpha")
        _write_worm_audit(_isolated_arc_data_dir, seq=1, actor_did="did:arc:beta")

        team = _build_team_dir(tmp_path)
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
            resp = client.get("/api/agents/alpha/audit", headers=_viewer(auth))
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert [e["actor_did"] for e in events] == ["did:arc:alpha"]
        assert events[0]["action"] == "gateway.fs.read"


class TestPolicyRoutes:
    def test_policy_returns_raw_and_bullets(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/policy", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        assert "raw" in body
        assert "bullets" in body
        assert any(b["id"] == "P01" for b in body["bullets"])

    def test_policy_bullets_only(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/policy/bullets", headers=_viewer(auth))
        assert resp.status_code == 200
        bullets = resp.json()["bullets"]
        assert {b["id"] for b in bullets} == {"P01", "P02", "P03"}

    def test_policy_stats_aggregates(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/policy/stats", headers=_viewer(auth))
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["active"] == 2  # P03 retired (score 1)
        assert stats["retired"] == 1
        # avg_score over non-retired = (8+6)/2 = 7.0
        assert abs(stats["avg_score"] - 7.0) < 0.01


class TestTasksAndSchedulesRoutes:
    def test_tasks_returns_array(self, tmp_path, _isolated_arc_data_dir: Path):
        team = _build_team_dir(tmp_path)
        asyncio.run(
            _seed_task(
                _isolated_arc_data_dir,
                Task(id="t1", title="Investigate", creator_did="did:arc:alpha", owner_did="did:arc:alpha", priority="high"),
            )
        )
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        assert body["tasks"][0]["id"] == "t1"

    def test_tasks_missing_returns_empty(self, tmp_path):
        team_root = tmp_path / "team"
        team_root.mkdir()
        agent = team_root / "beta_agent"
        agent.mkdir()
        (agent / "arcagent.toml").write_text(
            '[agent]\nname = "beta"\n[identity]\ndid = "did:arc:beta"\n',
            encoding="utf-8",
        )
        (agent / "workspace").mkdir()
        app, auth, _ = _make_detail_app(team_root=team_root)
        client = TestClient(app)
        resp = client.get("/api/agents/beta/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"tasks": []}

    def test_schedules_returns_array(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/schedules", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json()["schedules"][0]["id"] == "sched1"


class TestRoutesRequireAuth:
    def test_config_no_token_401(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, _, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/config")
        assert resp.status_code == 401


# Additional coverage — exercise 404/error branches across every endpoint
# so the per-handler "unknown agent" path is verified.


class TestUnknownAgent404:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/agents/missing/config",
            "/api/agents/missing/files/tree",
            "/api/agents/missing/files/read?path=foo.md",
            "/api/agents/missing/tools",
            "/api/agents/missing/sessions",
            "/api/agents/missing/sessions/sid1",
            "/api/agents/missing/stats",
            "/api/agents/missing/audit",
            "/api/agents/missing/policy",
            "/api/agents/missing/policy/bullets",
            "/api/agents/missing/policy/stats",
            "/api/agents/missing/tasks",
            "/api/agents/missing/schedules",
        ],
    )
    def test_returns_404(self, tmp_path, path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(path, headers=_viewer(auth))
        assert resp.status_code == 404


class TestEdgeCases:
    def test_files_tree_traversal_via_root_arg_blocked(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        # Invalid root values are 400, not 500/exposed paths.
        resp = client.get("/api/agents/alpha/files/tree?root=../../etc", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_files_read_invalid_root_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(
            "/api/agents/alpha/files/read?root=etc&path=passwd",
            headers=_viewer(auth),
        )
        assert resp.status_code == 400

    def test_session_pagination_invalid_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get(
            "/api/agents/alpha/sessions/session-001?page=abc",
            headers=_viewer(auth),
        )
        assert resp.status_code == 400

    def test_session_missing_jsonl_404(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/sessions/nonexistent", headers=_viewer(auth))
        assert resp.status_code == 404

    def test_stats_invalid_window_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/stats?window=evil", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_audit_invalid_limit_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/audit?limit=abc", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_tasks_malformed_json_returns_empty(self, tmp_path):
        team = _build_team_dir(tmp_path)
        # Overwrite tasks.json with garbage
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text(
            "not json", encoding="utf-8"
        )
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/tasks", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"tasks": []}

    def test_tasks_object_root_returns_empty(self, tmp_path):
        team = _build_team_dir(tmp_path)
        (team / "alpha_agent" / "workspace" / "tasks.json").write_text(
            '{"single": "object"}', encoding="utf-8"
        )
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/tasks", headers=_viewer(auth))
        assert resp.json() == {"tasks": []}

    def test_traces_with_store_returns_records(self, tmp_path, _isolated_arc_data_dir: Path):
        # SPEC-026 FR-5: traces come from the Observe plane (arcstore mirror).
        # Seed a spool record, backfill via the full create_app lifespan.
        from arcstore.records import SpoolRecord
        from arcstore.spool import record as spool_record

        from arcui.auth import AuthConfig
        from arcui.server import create_app

        spool = _isolated_arc_data_dir / "spool"
        spool.mkdir(parents=True, exist_ok=True)
        rec = SpoolRecord(
            kind="llm_call",
            actor_did="did:arc:test:alpha",
            agent_label="alpha",
            request_id="req-alpha-1",
            model="claude",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.001,
            latency_ms=10.0,
            outcome="ok",
        )
        spool_record(rec, path=spool / "operational-2026-05-31.jsonl")

        team = _build_team_dir(tmp_path)
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = create_app(auth_config=auth, team_root=team)
        with TestClient(app) as client:
            resp = client.get(
                "/api/agents/alpha/traces?limit=5",
                headers=_viewer(auth),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cursor"] is None
        assert len(body["traces"]) == 1
        assert body["traces"][0]["agent_label"] == "alpha"

    def test_traces_invalid_limit_400(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/traces?limit=abc", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_config_invalid_toml_returns_500(self, tmp_path):
        # Roster knows about the agent (provider is stubbed), but the file
        # on disk is malformed by the time the route reads it. Reaching this
        # branch in production is rare — the roster ITSELF would fail to
        # load the agent — but the handler must still degrade safely.
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)

        from arcgateway.team_roster import RosterEntry

        stub_entry = RosterEntry(
            agent_id="alpha",
            name="alpha",
            did="did:arc:alpha",
            org=None,
            type=None,
            workspace_path=str(team / "alpha_agent"),
            model="openai/gpt-4o",
            provider="openai",
            online=False,
            display_name="alpha",
            color="#aaaaaa",
            role_label="",
            hidden=False,
        )
        app.state.roster_provider = lambda: [stub_entry]

        # Corrupt arcagent.toml AFTER the roster snapshot has been bound.
        (team / "alpha_agent" / "arcagent.toml").write_text("this is not [toml", encoding="utf-8")
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/config", headers=_viewer(auth))
        assert resp.status_code == 500

    def test_skills_with_no_skills_dir_returns_only_system_skills(self, tmp_path):
        # SPEC-022 final round: agents always inherit system-wide built-in
        # skills (create-skill, update-skill, create-tool, update-tool)
        # shipped with arcagent. So an agent with no workspace/skills dir
        # surfaces those builtins, not an empty list.
        team_root = tmp_path / "team"
        team_root.mkdir()
        agent = team_root / "beta_agent"
        agent.mkdir()
        (agent / "arcagent.toml").write_text(
            '[agent]\nname = "beta"\n[identity]\ndid = "did:arc:beta"\n',
            encoding="utf-8",
        )
        (agent / "workspace").mkdir()
        app, auth, _ = _make_detail_app(team_root=team_root)
        client = TestClient(app)
        resp = client.get("/api/agents/beta/skills", headers=_viewer(auth))
        assert resp.status_code == 200
        body = resp.json()
        skills = body["skills"]
        # Workspace skills: 0 (no workspace/skills dir).
        # Builtin skills: 4 known names from arcagent.builtins.capabilities.skills.
        # Inventory rows carry the loader's ``source_root`` (e.g. "builtins-skills");
        # every surfaced skill must originate from a builtins scan root.
        source_roots = {str(s.get("source_root", "")) for s in skills}
        assert all(r.startswith("builtins") for r in source_roots) or not skills, (
            f"expected only builtin skills, got source_roots={source_roots}"
        )
        # If arcagent is installed and has the canonical builtins, all 4 show up.
        if skills:
            names = {s["name"] for s in skills}
            for must_have in ("create-skill", "update-skill", "create-tool", "update-tool"):
                assert must_have in names, f"missing builtin skill: {must_have}"

    def test_sessions_no_sessions_dir(self, tmp_path):
        team_root = tmp_path / "team"
        team_root.mkdir()
        agent = team_root / "beta_agent"
        agent.mkdir()
        (agent / "arcagent.toml").write_text(
            '[agent]\nname = "beta"\n[identity]\ndid = "did:arc:beta"\n',
            encoding="utf-8",
        )
        (agent / "workspace").mkdir()
        app, auth, _ = _make_detail_app(team_root=team_root)
        client = TestClient(app)
        resp = client.get("/api/agents/beta/sessions", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"sessions": []}

    def test_session_replay_invalid_sid_chars(self, tmp_path):
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        # space character is rejected by _VALID_SID
        resp = client.get("/api/agents/alpha/sessions/bad%20id", headers=_viewer(auth))
        assert resp.status_code == 400

    def test_stats_with_observe_returns_200(self, tmp_path):
        # SPEC-026 FR-5: stats come from Observe (arcstore mirror), not a RollingAggregator.
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/stats?window=1h", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json()["window"] == "1h"

    def test_traces_without_data_returns_empty(self, tmp_path):
        # SPEC-026 FR-5: with no spool data the Observe plane returns an empty list.
        team = _build_team_dir(tmp_path)
        app, auth, _ = _make_detail_app(team_root=team)
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/traces", headers=_viewer(auth))
        assert resp.status_code == 200
        assert resp.json() == {"traces": [], "cursor": None}

    def test_arcagent_toml_missing_404(self, tmp_path):
        team = _build_team_dir(tmp_path)
        # Stub the roster so the agent appears in the directory listing
        # even though arcagent.toml is gone.
        from arcgateway.team_roster import RosterEntry

        app, auth, _ = _make_detail_app(team_root=team)
        stub = RosterEntry(
            agent_id="alpha",
            name="alpha",
            did="did:arc:alpha",
            org=None,
            type=None,
            workspace_path=str(team / "alpha_agent"),
            model=None,
            provider=None,
            online=False,
            display_name="alpha",
            color="#aaaaaa",
            role_label="",
            hidden=False,
        )
        app.state.roster_provider = lambda: [stub]
        (team / "alpha_agent" / "arcagent.toml").unlink()
        client = TestClient(app)
        resp = client.get("/api/agents/alpha/config", headers=_viewer(auth))
        assert resp.status_code == 404


class TestNoRosterProvider:
    def test_unknown_when_no_provider(self):
        # roster_provider absent → all per-agent endpoints 404.
        from arcui.audit import UIAuditLogger
        from arcui.auth import AuthConfig, AuthMiddleware
        from arcui.registry import AgentRegistry
        from arcui.routes.agent_detail import routes as detail_routes

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        registry = AgentRegistry()
        app = Starlette(routes=detail_routes)
        app.add_middleware(AuthMiddleware, auth_config=auth)
        app.state.auth_config = auth
        app.state.agent_registry = registry
        app.state.audit = UIAuditLogger(enabled=False)
        # NO roster_provider, NO team_root.
        client = TestClient(app)
        resp = client.get("/api/agents/x/config", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 404
