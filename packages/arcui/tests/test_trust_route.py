"""``/api/trust/*`` — operator-gated capability-trust surface (SPEC-021).

GET lists gated capabilities across the roster (any role) via the arcagent
inventory seam; POST approve/disapprove is operator-only and mutates the agent's
``arcagent.toml`` through the arctrust approval store. A viewer is refused; an
operator flips a first-sight signed capability from ``new_sighting`` to
``loaded`` and can revoke it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.capabilities import artifact_signing
from arcgateway import team_roster
from arctrust import OperatorKey
from arctrust.identity import AgentIdentity
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.trust import routes as trust_routes

_VALID_SKILL = (
    "---\n"
    "name: {name}\n"
    "version: 2.0.0\n"
    "description: does {name}\n"
    "triggers: [{name}]\n"
    "tools: [reload]\n"
    "---\n"
    "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
    "## Anti Patterns\n\n## Examples\n\n## Validation\n"
)


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # arctrust.arc_home() (operator-key resolution) and load_config's base both
    # follow ARC_CONFIG_DIR — pin it at the test tmp so nothing touches ~/.arc.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))


def _bootstrap_operator_key(tmp_path: Path) -> None:
    key_path = tmp_path / "arc" / "operator" / "operator.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    OperatorKey.load(key_path, generate_if_absent=True)


def _build_agent(team_root: Path, name: str, *, tier: str, sign: bool) -> None:
    agent_dir = team_root / name
    skills = agent_dir / "workspace" / "capabilities" / "skills"
    skills.mkdir(parents=True)
    key_dir = team_root / f"{name}-keys"
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    identity.save_keys(key_dir)

    folder = skills / "reporter"
    folder.mkdir()
    skill_md = folder / "SKILL.md"
    content = _VALID_SKILL.format(name="reporter").encode("utf-8")
    skill_md.write_bytes(content)
    if sign:
        artifact_signing.write_signature(
            skill_md, content, signer_did=identity.did, private_key=identity.signing_seed
        )
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        f'[security]\ntier = "{tier}"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )


def _make_client(team_root: Path) -> TestClient:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=trust_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app)


_VIEWER = {"Authorization": "Bearer viewer"}
_OPERATOR = {"Authorization": "Bearer operator"}


def test_get_gated_lists_new_sighting(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    client = _make_client(team_root)

    resp = client.get("/api/trust/gated", headers=_VIEWER)
    assert resp.status_code == 200
    gated = resp.json()["gated"]
    item = next(it for it in gated if it["name"] == "reporter")
    assert item["kind"] == "skill"
    assert item["status"] == "new_sighting"
    assert item["agent_id"] == "olivia"
    assert set(item) == {
        "agent_id", "agent_label", "name", "kind", "status", "path", "hash", "detail"
    }


def test_approve_requires_operator(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_VIEWER, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "operator_role_required"


def test_operator_approve_then_disapprove_round_trip(tmp_path: Path) -> None:
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    client = _make_client(team_root)

    approve = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert approve.status_code == 200
    body = approve.json()
    assert body["status"] == "loaded"
    assert body["name"] == "reporter"

    # Now loaded -> no longer gated.
    gated = client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
    assert "reporter" not in {it["name"] for it in gated}

    disapprove = client.post(
        "/api/trust/disapprove", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert disapprove.status_code == 200
    assert disapprove.json() == {"ok": True}

    # Revoked -> gated again.
    gated_again = client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
    assert "reporter" in {it["name"] for it in gated_again}


def test_approve_unknown_capability_is_404(tmp_path: Path) -> None:
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=True)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "ghost"}
    )
    assert resp.status_code == 404


def test_approve_unknown_agent_is_404(tmp_path: Path) -> None:
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "ghost", "name": "reporter"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "agent_not_found"
