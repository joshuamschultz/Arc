"""Regression: SPEC agent-detail U6 — the tool detail drawer's backing route.

`GET /api/agents/{id}/tools/{tool_name}/detail` locates a tool's `.py` source
across the same on-disk roots `_collect_disk_tools` scans (agent/workspace-
authored tools), falls back to the capability inventory seam, then to the
arcagent builtins dir, and computes whether/where the UI can save an edit
back through the existing `PUT /files/read` route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

_TOOL_SRC = """\
from arcagent.builtins.capabilities import tool


@tool(name="word_count", description="Count words", classification="read_only")
async def word_count(text: str) -> str:
    return str(len(text.split()))
"""


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))


def _agent(tmp_path: Path) -> tuple[TestClient, str, Path]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia_agent"
    workspace = agent_dir / "workspace"
    # Where create_tool writes agent-authored tools at runtime.
    caps = workspace / "capabilities"
    caps.mkdir(parents=True)
    (caps / "word_count.py").write_text(_TOOL_SRC, encoding="utf-8")

    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{workspace}"\n'
        '[llm]\nmodel = "test/model"\n[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )

    auth = AuthConfig({"viewer_token": "viewer"})
    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.embedded_agent_cache = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app), "olivia", agent_dir


def _detail(client: TestClient, agent_id: str, tool_name: str):
    return client.get(
        f"/api/agents/{agent_id}/tools/{tool_name}/detail",
        headers={"Authorization": "Bearer viewer"},
    )


def test_workspace_authored_tool_is_editable(tmp_path: Path) -> None:
    client, agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, agent_id, "word_count")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "word_count"
    assert body["transport"] == "workspace"
    assert body["classification"] == "read_only"
    assert "def word_count" in body["content"]
    assert body["editable"] is True
    assert body["write_root"] == "workspace"
    assert body["write_path"] == "capabilities/word_count.py"


def test_builtin_tool_is_read_only(tmp_path: Path) -> None:
    client, agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, agent_id, "read")
    assert resp.status_code == 200
    body = resp.json()
    assert body["transport"] == "builtin"
    assert "def read" in body["content"] or "async def read" in body["content"]
    assert body["editable"] is False
    assert body["write_root"] is None
    assert body["write_path"] is None


def test_unknown_tool_is_404(tmp_path: Path) -> None:
    client, agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, agent_id, "does-not-exist")
    assert resp.status_code == 404
    assert "does-not-exist" in resp.json()["error"]


def test_unknown_agent_is_404(tmp_path: Path) -> None:
    client, _agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, "nobody", "word_count")
    assert resp.status_code == 404


def test_module_tool_returns_read_only_detail(tmp_path: Path) -> None:
    """A module-derived tool (e.g. schedule_update) has no editable file, but it IS in
    the Tools list — so detail must surface a READ-ONLY view, not a 404 dead-end."""
    client, agent_id, agent_dir = _agent(tmp_path)
    toml = agent_dir / "arcagent.toml"
    toml.write_text(toml.read_text() + "\n[modules.scheduler]\nenabled = true\n", encoding="utf-8")

    resp = _detail(client, agent_id, "schedule_update")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "schedule_update"
    assert body["transport"] == "module:scheduler"
    assert body["editable"] is False
    assert body["write_root"] is None and body["write_path"] is None
