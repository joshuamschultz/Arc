"""SPEC-083 T-1226 (REQ-513, COMP-030) — ArcUI never shows a rejected override as effective.

The agent verifies every override against the pinned operator key before its text can
reach a model, and a broken override fails the run closed (it never silently becomes
stock). ArcUI's prompt view must tell the operator the same story: an override whose
signature is missing, does not match its bytes, or was made with another key is
**rejected** — not "overridden", and its text is not presented as the effective prompt.

Real Starlette app + real agent dir + real arcprompt catalog + the real on-box
operator key, following ``test_prompts_route.py``'s fixture shape. Each override is
first written through ArcUI's own PUT route (so it starts out genuinely signed) and
then damaged on disk the way an attacker with folder access would.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcgateway import team_roster
from arcprompt import load_stock_document
from arctrust import OperatorKey, default_operator_key_path
from arctrust.artifact import sign_artifact
from arctrust.identity import AgentIdentity
from arctrust.keypair import generate_keypair
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

_PKG, _NAME = "arcagent", "base_system"
_OVERRIDE = "Operator override body for the overlay-status test."


@pytest.fixture
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "archome"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    key_dir = tmp_path / "keys"
    identity.save_keys(key_dir)
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia_agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nname = "olivia"\norg = "arc"\ntype = "exec"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n[security]\ntier = "personal"\n'
        f'[identity]\ndid = "{identity.did}"\nkey_dir = "{key_dir}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.embedded_agent_cache = None
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app), agent_dir


def _put_signed(client: TestClient) -> None:
    resp = client.put(
        f"/api/agents/olivia/prompts/{_PKG}/{_NAME}",
        json={"content": _OVERRIDE},
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 200, resp.text


def _overlay(agent_dir: Path) -> Path:
    return agent_dir / "context" / _PKG / f"{_NAME}.md"


def _sidecar(agent_dir: Path) -> Path:
    return Path(f"{_overlay(agent_dir)}.arcsig")


def _listed_status(client: TestClient) -> str:
    items = client.get(
        "/api/agents/olivia/prompts", headers={"Authorization": "Bearer viewer"}
    ).json()["items"]
    (item,) = [i for i in items if i["package"] == _PKG and i["name"] == _NAME]
    return str(item["status"])


def _detail(client: TestClient) -> dict[str, object]:
    resp = client.get(
        f"/api/agents/olivia/prompts/{_PKG}/{_NAME}", headers={"Authorization": "Bearer viewer"}
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, object] = resp.json()
    return body


def _assert_rejected(client: TestClient, shown_text: str) -> None:
    assert _listed_status(client) == "rejected"
    detail = _detail(client)
    assert detail["status"] == "rejected"
    assert shown_text not in str(detail["effective"]), (
        "a rejected override's text is presented as the prompt the agent uses"
    )


# --- Control: a genuine ArcUI-signed override is effective --------------------------


def test_signed_override_is_reported_overridden_and_effective(
    ui: tuple[TestClient, Path],
) -> None:
    client, _agent_dir = ui
    _put_signed(client)
    assert _listed_status(client) == "overridden"
    detail = _detail(client)
    assert detail["status"] == "overridden"
    assert detail["effective"] == _OVERRIDE


def test_no_override_is_reported_stock(ui: tuple[TestClient, Path]) -> None:
    client, _agent_dir = ui
    assert _listed_status(client) == "stock"
    assert _detail(client)["effective"] == load_stock_document(_PKG, _NAME).body


# --- Rejected overrides ---------------------------------------------------------------


def test_override_with_missing_signature_is_rejected(ui: tuple[TestClient, Path]) -> None:
    client, agent_dir = ui
    _put_signed(client)
    _sidecar(agent_dir).unlink()
    _assert_rejected(client, _OVERRIDE)


def test_override_edited_after_signing_is_rejected(ui: tuple[TestClient, Path]) -> None:
    """Direct file tampering: the bytes no longer match the operator's signature."""
    client, agent_dir = ui
    _put_signed(client)
    tampered = "Tampered text an attacker wrote straight into the agent folder."
    overlay = _overlay(agent_dir)
    overlay.write_text(overlay.read_text(encoding="utf-8").replace(_OVERRIDE, tampered))
    _assert_rejected(client, tampered)


def test_override_signed_with_another_key_is_rejected(ui: tuple[TestClient, Path]) -> None:
    """A self-consistent signature from a key that is not the pinned operator key."""
    client, agent_dir = ui
    _put_signed(client)
    overlay = _overlay(agent_dir)
    forged = "Forged override signed by an attacker-held key."
    raw = overlay.read_bytes().replace(_OVERRIDE.encode(), forged.encode())
    overlay.write_bytes(raw)
    attacker = generate_keypair()
    manifest = sign_artifact(raw, signer_did="did:arc:attacker", private_key=attacker.private_key)
    _sidecar(agent_dir).write_text(manifest.to_json(), encoding="utf-8")
    _assert_rejected(client, forged)


def test_override_with_unparseable_signature_is_rejected(ui: tuple[TestClient, Path]) -> None:
    client, agent_dir = ui
    _put_signed(client)
    _sidecar(agent_dir).write_text("{not a signature", encoding="utf-8")
    _assert_rejected(client, _OVERRIDE)
