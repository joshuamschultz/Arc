"""Regression: SPEC agent-detail U5 — the skill detail drawer's backing route.

`GET /api/agents/{id}/skills/{skill_name}/detail` resolves a skill through the
same arcagent capability inventory seam the Skills tab list uses
(`agent_skill_rows`), reads its SKILL.md body, and computes whether/where the
UI can save an edit back through the existing `PUT /files/read` route. Mirrors
`test_skills_scan.py`'s fixture pattern (real Starlette app + real agent dir).
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


def _agent(tmp_path: Path) -> tuple[TestClient, str, Path]:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia_agent"
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    for name in ("alpha",):
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
    return TestClient(app), "olivia", agent_dir


def _detail(client: TestClient, agent_id: str, skill_name: str):
    return client.get(
        f"/api/agents/{agent_id}/skills/{skill_name}/detail",
        headers={"Authorization": "Bearer viewer"},
    )


def test_workspace_skill_is_editable(tmp_path: Path) -> None:
    client, agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, agent_id, "alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "alpha"
    assert body["source_root"] == "workspace-skills"
    assert "name: alpha" in body["content"]
    assert body["editable"] is True
    assert body["write_root"] == "workspace"
    assert body["write_path"] == "capabilities/skills/alpha/SKILL.md"


def test_builtin_skill_is_read_only(tmp_path: Path) -> None:
    client, agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, agent_id, "create-skill")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_root"].startswith("builtins")
    assert body["content"]  # builtin SKILL.md body is readable
    assert body["editable"] is False
    assert body["write_root"] is None
    assert body["write_path"] is None


def test_unknown_skill_is_404(tmp_path: Path) -> None:
    client, agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, agent_id, "does-not-exist")
    assert resp.status_code == 404
    assert "does-not-exist" in resp.json()["error"]


def test_unknown_agent_is_404(tmp_path: Path) -> None:
    client, _agent_id, _agent_dir = _agent(tmp_path)
    resp = _detail(client, "nobody", "alpha")
    assert resp.status_code == 404
