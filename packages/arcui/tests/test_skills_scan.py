"""Regression: the Skills tab lists what the agent actually loads.

Agent-authored skills live at ``<agent>/workspace/capabilities/skills/<name>/``
(where ``create_skill``/``update_skill`` write). arcui no longer globs for them
— ``GET /api/agents/{id}/skills`` runs the arcagent inventory seam, which scans
that path via the ``workspace-skills`` loader root and reports each skill's
source root and verbatim load status. These tests lock the route onto that
faithful path across authored + builtin skills.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.capabilities import artifact_signing
from arcgateway import team_roster
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

_SKILL = (
    "---\nname: {name}\nversion: 1.0.0\ndescription: does {name}\n"
    "triggers: [{name}]\ntools: [reload]\n---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))


def _agent(tmp_path: Path) -> tuple[TestClient, str]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia_agent"
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    for name in ("alpha", "beta"):
        folder = skills / name
        folder.mkdir()
        skill_md = folder / "SKILL.md"
        content = _SKILL.format(name=name).encode("utf-8")
        skill_md.write_bytes(content)
        artifact_signing.write_signature(
            skill_md, content, signer_did=identity.did, private_key=identity.signing_seed
        )
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
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
    return TestClient(app), "olivia"


def _skills(tmp_path: Path) -> list[dict]:
    client, agent_id = _agent(tmp_path)
    resp = client.get(f"/api/agents/{agent_id}/skills", headers={"Authorization": "Bearer viewer"})
    assert resp.status_code == 200
    return resp.json()["skills"]


def test_authored_skills_surface_with_source_root(tmp_path: Path) -> None:
    rows = {s["name"]: s for s in _skills(tmp_path)}
    assert {"alpha", "beta"} <= set(rows)
    assert rows["alpha"]["source_root"] == "workspace-skills"
    assert rows["alpha"]["status"] == "loaded"


def test_builtin_skills_still_surface(tmp_path: Path) -> None:
    names = {s["name"] for s in _skills(tmp_path)}
    # The four builtin self-mod skills load from the trusted builtins-skills root.
    assert {"create-skill", "update-skill", "create-tool", "update-tool"} <= names
