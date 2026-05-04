"""Integration tests for ``GET /api/knowledge/{agent_id}``.

The route is a thin assembler over arcgateway.fs_reader. These tests
build a fake team/ directory on disk, stub the roster_provider, and
verify the JSON shape from SDD §5.2.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-knowledge"


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def _make_agent_dir(team_root: Path, name: str) -> Path:
    """Create a minimal ``<name>_agent/`` directory with memory + workspace."""
    agent_dir = team_root / f"{name}_agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text("[agent]\nname = '" + name + "'\n")
    memory = agent_dir / "memory"
    memory.mkdir()
    (memory / "context.md").write_text("# Context\n\nFirst memory entry.")
    (memory / "notes.md").write_text("Some notes here.")
    workspace = agent_dir / "workspace"
    workspace.mkdir()
    (workspace / "plan.md").write_text("# Plan\n")
    (workspace / "notes").mkdir()
    (workspace / "notes" / "entry.md").write_text("nested note\n")
    return agent_dir


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Any]:
    team_root = tmp_path / "team"
    team_root.mkdir()
    _make_agent_dir(team_root, "concierge")

    auth = AuthConfig(
        {"viewer_token": VIEWER_TOKEN, "operator_token": "op", "agent_token": "ag"}
    )
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="concierge",
            name="concierge",
            did="did:arc:agent:concierge",
            org=None,
            type="agent",
            workspace_path=str(team_root / "concierge_agent"),
            model="claude-3-5-sonnet",
            provider="anthropic",
            online=True,
            display_name="Concierge",
            color="#1abc9c",
            role_label="Test",
            hidden=False,
        )
    ]
    yield app


def test_knowledge_api_returns_correct_shape(app: Any) -> None:
    """Response matches the SDD §5.2 shape."""
    with TestClient(app) as client:
        resp = client.get("/api/knowledge/concierge", headers=_auth_header())
        assert resp.status_code == 200
        data = resp.json()
        # Top-level
        assert data["agent_id"] == "concierge"
        assert data["agent_did"] == "did:arc:agent:concierge"
        assert "context" in data
        assert "memory" in data
        assert "workspace" in data
        assert "graph" in data

        # Memory entries — sorted alphabetically (context.md before notes.md)
        entries = data["memory"]["entries"]
        assert len(entries) == 2
        names = [e["filename"] for e in entries]
        assert names == ["context.md", "notes.md"]
        assert entries[0]["size_bytes"] > 0
        assert entries[0]["preview"].startswith("# Context")
        assert data["memory"]["total_bytes"] > 0
        assert isinstance(data["memory"]["recent_events"], list)

        # Workspace tree — at least the top-level plan.md + notes/ folder
        paths = {n["path"] for n in data["workspace"]["tree"]}
        assert any("plan.md" in p for p in paths)
        assert any(p.startswith("workspace/notes") for p in paths)


def test_knowledge_endpoint_404_when_agent_missing(app: Any) -> None:
    """Unknown agent_id ⇒ 404 with an error body."""
    with TestClient(app) as client:
        resp = client.get("/api/knowledge/ghost", headers=_auth_header())
        assert resp.status_code == 404
        assert "ghost" in resp.json()["error"]


def test_knowledge_endpoint_graph_unavailable_returns_200(app: Any) -> None:
    """When the code-graph MCP isn't wired the route still returns 200."""
    with TestClient(app) as client:
        resp = client.get("/api/knowledge/concierge", headers=_auth_header())
        assert resp.status_code == 200
        assert resp.json()["graph"] == {"available": False}


def test_knowledge_endpoint_requires_auth(app: Any) -> None:
    """Bearer token is required — same as the rest of /api/."""
    with TestClient(app) as client:
        resp = client.get("/api/knowledge/concierge")
        assert resp.status_code == 401
