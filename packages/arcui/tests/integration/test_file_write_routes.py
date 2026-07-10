"""Agent workspace file editor — PUT save (COMP-012 / REQ-099).

Extends the read surface with an operator-gated write on the same file
resource. Drives the real Starlette app with an on-disk agent root: escape
attempts and secret-shaped content are refused (400, audited), a viewer is
refused (403), a legit save lands on disk (200, audited), and a save over a
signed file flags the now-stale ``.arcsig``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes
from arcui.routes.agents import routes as agent_routes


def _build_team_dir(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    agent = root / "alpha_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\n[identity]\ndid = "did:arc:alpha"\n',
        encoding="utf-8",
    )
    ws = agent / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("# original persona\n", encoding="utf-8")
    return root


def _build_app(team_root: Path) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    registry = AgentRegistry()
    app = Starlette(routes=[*agent_routes, *agent_detail_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.session_tracker = SessionTracker()
    app.state.team_root = team_root

    def _roster_provider() -> list[team_roster.RosterEntry]:
        online = {a.agent_id for a in registry.list_agents()}
        return team_roster.list_team(team_root=team_root, online_ids=online)

    app.state.roster_provider = _roster_provider
    return app, auth


@pytest.fixture
def ctx(tmp_path: Path) -> tuple[TestClient, Path]:
    team_root = _build_team_dir(tmp_path)
    app, _ = _build_app(team_root)
    return TestClient(app), team_root / "alpha_agent"


def _op() -> dict[str, str]:
    return {"Authorization": "Bearer op"}


def _viewer() -> dict[str, str]:
    return {"Authorization": "Bearer viewer"}


_URL = "/api/agents/alpha/files/read?root=workspace&path=identity.md"


class TestSave:
    def test_operator_saves_file(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.put(_URL, headers=_op(), json={"content": "# new persona\n"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["signature_stale"] is False
        assert body["message"] == "Saved."
        assert (agent_dir / "workspace" / "identity.md").read_text() == "# new persona\n"

    def test_can_create_file_at_agent_root(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.put(
            "/api/agents/alpha/files/read?root=agent&path=NOTES.md",
            headers=_op(),
            json={"content": "notes\n"},
        )
        assert resp.status_code == 200
        assert (agent_dir / "NOTES.md").read_text() == "notes\n"

    def test_audits_applied(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = ctx
        with caplog.at_level("INFO", logger="arcui.audit"):
            client.put(_URL, headers=_op(), json={"content": "# x\n"})
        events = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "file_write"
            and e["details"]["outcome"] == "applied"
            and e["details"]["target"] == "workspace:identity.md"
            for e in events
        )


class TestGuards:
    def test_viewer_forbidden(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        resp = client.put(_URL, headers=_viewer(), json={"content": "hacked\n"})
        assert resp.status_code == 403
        # Unchanged on disk.
        assert (agent_dir / "workspace" / "identity.md").read_text() == "# original persona\n"

    @pytest.mark.parametrize(
        "bad_path",
        ["../../etc/passwd", "/etc/passwd", "../arcagent.toml", "subdir/../../escape"],
    )
    def test_path_escape_400(self, ctx: tuple[TestClient, Path], bad_path: str) -> None:
        client, _ = ctx
        resp = client.put(
            f"/api/agents/alpha/files/read?root=workspace&path={bad_path}",
            headers=_op(),
            json={"content": "x\n"},
        )
        assert resp.status_code == 400

    def test_secret_content_refused_and_audited(
        self, ctx: tuple[TestClient, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        client, agent_dir = ctx
        # AWS access key id shape — matches arcllm SECRET_PATTERNS.
        payload = "aws creds\nAKIAIOSFODNN7EXAMPLE\n"
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.put(_URL, headers=_op(), json={"content": payload})
        assert resp.status_code == 400
        assert "credential" in resp.json()["error"]
        # The refusal never wrote the payload to disk.
        assert (agent_dir / "workspace" / "identity.md").read_text() == "# original persona\n"
        denied = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert any(
            e["details"]["operation"] == "file_write" and e["details"]["outcome"] == "denied"
            for e in denied
        )

    def test_generic_labeled_token_refused(self, ctx: tuple[TestClient, Path]) -> None:
        client, _ = ctx
        payload = "config\napi_key = 'bb_live_0123456789abcdef0123'\n"
        resp = client.put(_URL, headers=_op(), json={"content": payload})
        assert resp.status_code == 400


class TestArcsigStaleness:
    def test_saving_signed_file_flags_stale(self, ctx: tuple[TestClient, Path]) -> None:
        client, agent_dir = ctx
        # A signed capability with its detached sidecar.
        cap = agent_dir / "workspace" / "signed_skill.md"
        cap.write_text("# skill\n", encoding="utf-8")
        (agent_dir / "workspace" / "signed_skill.md.arcsig").write_text("sig", encoding="utf-8")

        resp = client.put(
            "/api/agents/alpha/files/read?root=workspace&path=signed_skill.md",
            headers=_op(),
            json={"content": "# edited skill\n"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["signature_stale"] is True
        assert "re-sign" in body["message"]
