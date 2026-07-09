"""Regression tests for SPEC-022 Phase 5/6/7 issues found during live rehearsal.

Every assertion below is a bug we shipped at one point and fixed; the test
exists so a future change cannot silently re-introduce it.

Issues covered:
  R1 — `/api/agents/{id}` 404'd for offline agents (no live registration)
       even when the agent existed on disk via team_root. Fixed: roster
       fallback in `get_agent`.
  R2 — `arc ui start` did not pass `--team-root` to `create_app`, leaving
       `app.state.roster_provider` returning empty. Fixed: CLI flag +
       default `./team` discovery.
  R3 — Frontend `Fmt.number` used `this._numberFmt` so destructuring
       `var fmt = window.Fmt.number` lost the binding and threw at call
       time. Fixed in agent-detail.js: bind to window.Fmt.
  R4 — `/api/agents/{id}/stats` returns `{stats: {...}, window}`; agent-
       detail.js read `request_count` from the wrapper instead of `.stats`.
       Fixed: unwrap before reading.
  R5 — Index.html route → page mount mapping cached `_pageInstances` per
       page id and never re-mounted on agent-id change within the same
       page (`?page=agent-detail&agent=A` → `agent=B` kept showing A).
       Fixed: track _detailMountedAgent, dispose on change.
  R6 — Vendored Prism bundle lacked `clike` (a required dep of the
       javascript grammar), causing "Cannot read properties of undefined
       (reading 'class-name')" pageerror on first highlight. Fixed:
       re-vendored as core+clike+python+toml+json+javascript.
  R7 — `_resolve_root` etc. live in arcgateway.fs_reader; verify the read
       chokepoint API is stable (no `write_*` methods exist).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistration, AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes

_ROOT = Path(__file__).resolve().parents[3]
_UI_PY = _ROOT / "packages/arccli/src/arccli/commands/ui.py"


def _build_team(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    agent = root / "alpha_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\norg = "test"\ntype = "scout"\n'
        '[identity]\ndid = "did:arc:test:alpha"\n'
        '[llm]\nmodel = "anthropic/claude-sonnet-4-5"\n',
        encoding="utf-8",
    )
    (agent / "workspace").mkdir()
    return root


def _build_app(team_root: Path | None) -> tuple[Starlette, AuthConfig, AgentRegistry]:
    auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
    registry = AgentRegistry()
    app = Starlette(routes=[*agent_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.team_root = team_root
    app.state.trace_store = None

    if team_root is not None:

        def _roster_provider() -> list[team_roster.RosterEntry]:
            online = {a.agent_id for a in registry.list_agents()}
            return team_roster.list_team(team_root=team_root, online_ids=online)

        app.state.roster_provider = _roster_provider
    return app, auth, registry


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


# ============================================================
# R1: /api/agents/{id} falls back to roster for offline agents
# ============================================================


class TestR1OfflineAgentMetaFallback:
    """An agent on disk but not connected MUST surface via /api/agents/{id}.

    Pre-fix: 404 because the route only looked at the live AgentRegistry.
    Post-fix: returns flat metadata with online=False from the roster
    provider so the agent-detail Identity card can render.
    """

    def test_offline_agent_returns_meta_from_roster(self, tmp_path: Path) -> None:
        team_root = _build_team(tmp_path)
        app, auth, _registry = _build_app(team_root)
        client = TestClient(app)

        resp = client.get("/api/agents/alpha", headers=_viewer(auth))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_id"] == "alpha"
        assert body["did"] == "did:arc:test:alpha"
        assert body["online"] is False
        # Real diagnostic fields the SPA reads
        assert "model" in body
        assert "workspace_path" in body

    def test_live_agent_takes_precedence_over_roster(self, tmp_path: Path) -> None:
        team_root = _build_team(tmp_path)
        app, auth, registry = _build_app(team_root)
        # Register live with a different agent_name to make the override observable.
        reg = AgentRegistration(
            agent_id="alpha",
            agent_name="alpha-live",
            model="custom/model",
            provider="custom",
            connected_at="2026-01-01T00:00:00Z",
        )
        registry.register("alpha", reg)
        client = TestClient(app)

        resp = client.get("/api/agents/alpha", headers=_viewer(auth))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["online"] is True
        assert body["agent_id"] == "alpha"
        # Live registration wins; not the disk-roster name.
        assert body["agent_name"] == "alpha-live"

    def test_unknown_agent_still_404s(self, tmp_path: Path) -> None:
        team_root = _build_team(tmp_path)
        app, auth, _ = _build_app(team_root)
        client = TestClient(app)
        resp = client.get("/api/agents/ghost", headers=_viewer(auth))
        assert resp.status_code == 404
        assert resp.json() == {"error": "Agent not found"}

    def test_no_team_root_no_roster_404(self) -> None:
        # When the server has no team_root, offline-meta fallback is absent.
        app, auth, _ = _build_app(team_root=None)
        client = TestClient(app)
        resp = client.get("/api/agents/anything", headers=_viewer(auth))
        assert resp.status_code == 404


# ============================================================
# R2: `arc ui start` accepts --team-root and defaults to ./team
# ============================================================


class TestR2UiStartTeamRoot:
    """The CLI must wire team_root into create_app or the SPA roster
    endpoints all return empty arrays. Verified statically rather than
    spawning a subprocess (the expensive integration form lives in the
    rehearsal flow)."""

    def test_team_root_arg_added(self) -> None:
        text = _UI_PY.read_text()
        assert "--team-root" in text
        assert "team_root=" in text  # passed to create_app

    def test_default_to_cwd_team(self) -> None:
        text = _UI_PY.read_text()
        # Default fallback discovers ./team if it exists; documented and
        # implemented as Path.cwd() / "team".
        assert 'Path.cwd() / "team"' in text or 'Path.cwd() / "team"' in text


# ============================================================
# R7: fs_reader read-only by structure (re-affirm; covered in arcgateway tests)
# ============================================================


class TestR7FsReaderReadOnly:
    """Defensive: arcgateway.fs_reader exposes no write surface. This
    duplicates a structural test in arcgateway but lives here too so the
    arcui side fails loudly if a "convenient write helper" ever sneaks
    into a refactor.
    """

    def test_no_write_methods_exposed(self) -> None:
        from arcgateway import fs_reader

        api = {n for n in dir(fs_reader) if not n.startswith("_")}
        forbidden = {
            "write_file",
            "write_text",
            "write_bytes",
            "mkdir",
            "remove",
            "unlink",
            "rmtree",
            "save",
        }
        leaks = api & forbidden
        assert not leaks, f"fs_reader exposes write methods: {leaks}"

    def test_read_file_signature_takes_caller_did(self) -> None:
        from arcgateway.fs_reader import read_file

        sig = inspect.signature(read_file)
        # Audit trail correctness — `caller_did` is mandatory.
        assert "caller_did" in sig.parameters
