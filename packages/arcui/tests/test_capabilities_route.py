"""SPEC arcui-reality-mirror COMP-008 (T-711) — per-agent capability route.

`GET /api/agents/{id}/capabilities` mirrors what the agent actually loads: it
runs arcagent's capability inventory seam at the agent's real trust posture and
returns every skill / capability tool across the scan roots with its verbatim
loader verdict (source_root + status). A tofu-denied skill must appear with the
loader's own status string, never a UI-invented label (REQ-093/094/096).
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

_VALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 1.2.3\n"
    "description: does {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)
_INVALID_SKILL = (
    "---\nname: {name}\nversion: 1.0.0\ndescription: broken\n"
    "triggers: [{name}]\ntools: [reload]\n---\n\nno sections\n"
)


def _write_skill(skills_root: Path, name: str, *, sign_with: AgentIdentity | None) -> None:
    folder = skills_root / name
    folder.mkdir(parents=True)
    skill_md = folder / "SKILL.md"
    content = _VALID_SKILL.format(name=name).encode("utf-8")
    skill_md.write_bytes(content)
    if sign_with is not None:
        artifact_signing.write_signature(
            skill_md, content, signer_did=sign_with.did, private_key=sign_with.signing_seed
        )


def _build_agent(team_root: Path, name: str, identity: AgentIdentity, key_dir: Path) -> None:
    agent_dir = team_root / f"{name}_agent"
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    _write_skill(skills, "signed", sign_with=identity)
    _write_skill(skills, "unsigned", sign_with=None)
    (skills / "broken").mkdir()
    (skills / "broken" / "SKILL.md").write_text(_INVALID_SKILL.format(name="broken"))
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )


def _make_app(team_root: Path) -> tuple[TestClient, AgentRegistry]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    registry = AgentRegistry()
    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.team_root = team_root
    app.state.embedded_agent_cache = None

    def _roster_provider() -> list[team_roster.RosterEntry]:
        return team_roster.list_team(team_root=team_root, online_ids=set())

    app.state.roster_provider = _roster_provider
    return TestClient(app), registry


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty-arc"))


def test_capabilities_route_reports_verbatim_verdicts(tmp_path: Path) -> None:
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", identity, key_dir)

    client, _ = _make_app(team_root)
    resp = client.get(
        "/api/agents/olivia/capabilities", headers={"Authorization": "Bearer viewer"}
    )
    assert resp.status_code == 200
    body = resp.json()
    by_name = {it["name"]: it for it in body["items"]}

    assert by_name["signed"]["status"] == "loaded"
    assert by_name["signed"]["source_root"] == "workspace-skills"
    # TOFU denial rendered with the loader's verbatim verdict, not a UI literal.
    assert by_name["unsigned"]["status"] == "deny"
    assert by_name["broken"]["status"] == "invalid"
    # No live agent wired -> runtime tools absent, not fabricated.
    assert body["runtime"] is False


def test_capabilities_route_404_for_unknown_agent(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    team_root.mkdir()
    client, _ = _make_app(team_root)
    resp = client.get(
        "/api/agents/ghost_agent/capabilities", headers={"Authorization": "Bearer viewer"}
    )
    assert resp.status_code == 404
