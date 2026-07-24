"""`/api/agents/{id}/prompts/*` — list / inspect / override / reset system prompts.

Real Starlette app + real agent dir + real ``arcprompt`` catalog + real on-box
operator key (COMP-010/011/014). Mirrors ``test_agent_config_files_route.py``'s
fixture pattern; the operator-key setup mirrors ``test_approvals_route.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcgateway import team_roster
from arcprompt import PromptCatalog, load_stock_document
from arctrust import OperatorKey, default_operator_key_path
from arctrust.artifact import ArtifactSignature, verify_artifact
from arctrust.identity import AgentIdentity
from arctrust.keypair import generate_keypair
from arctrust.policy import Decision, PolicyContext, PolicyPipeline, ToolCall
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui import prompt_signing
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_routes

_SIDECAR = ".arcsig"


def _first_prompt() -> tuple[str, str]:
    """A real (package, name) from the installed catalog — the test never invents one."""
    refs = PromptCatalog().catalog()
    assert refs, "catalog empty — arcprompt not installed?"
    return refs[0].package, refs[0].name


def _agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, Path]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "archome"))
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
    app.state.roster_provider = lambda: team_roster.list_team(team_root=team_root, online_ids=set())
    return TestClient(app), "olivia", agent_dir


def _mk_operator_key() -> None:
    """Pre-create the on-box operator key the write path signs with."""
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)


def _resolved_operator_did() -> str:
    from arctrust.policy import OperatorApprovalAuthority

    key = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    return OperatorApprovalAuthority(key.into_signer()).did


def _put(client: TestClient, agent: str, pkg: str, name: str, content: str, token: str = "operator"):
    return client.put(
        f"/api/agents/{agent}/prompts/{pkg}/{name}",
        json={"content": content},
        headers={"Authorization": f"Bearer {token}"},
    )


_RUBRIC_PKG = "arcskill"
_RUBRIC_NAME = "judge_rubric"


def _put_rubric(client: TestClient, agent: str, body: object, token: str = "operator"):
    return client.put(
        f"/api/agents/{agent}/prompts/{_RUBRIC_PKG}/{_RUBRIC_NAME}/rubric",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def _get(client: TestClient, path: str, token: str = "viewer"):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


# --- Reads -----------------------------------------------------------------


def test_read_and_write_refuse_traversal_shaped_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-04: a name that is not a safe path component is refused by the arcprompt guard.

    A backslash survives URL routing where ``..`` would be normalized by the client;
    ``load_stock_document`` (guarded) rejects it before any overlay/stock read.
    """
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    bad = "a\\b"
    assert _get(client, f"/api/agents/{agent}/prompts/arcrun/{bad}").status_code == 404
    assert _put(client, agent, "arcrun", bad, "x").status_code in (400, 404)
    # No file was authored anywhere under the agent's context tree.
    context_dir = agent_dir / "context"
    assert not context_dir.exists() or not list(context_dir.rglob("*.md"))


def test_list_returns_every_stock_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    resp = _get(client, f"/api/agents/{agent}/prompts")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == len(PromptCatalog().catalog())
    assert all(i["status"] == "stock" for i in items)


def test_list_unknown_agent_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _agent(tmp_path, monkeypatch)
    assert _get(client, "/api/agents/nobody/prompts").status_code == 404


def test_detail_stock_has_empty_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    pkg, name = _first_prompt()
    body = _get(client, f"/api/agents/{agent}/prompts/{pkg}/{name}").json()
    assert body["status"] == "stock"
    assert body["stock"] == body["effective"]
    assert body["diff"] == ""
    assert body["stock"] == load_stock_document(pkg, name).body


def test_detail_unknown_prompt_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    assert _get(client, f"/api/agents/{agent}/prompts/arcagent/nope").status_code == 404


# --- Write / reset ---------------------------------------------------------


def test_put_writes_signed_overlay_with_resolved_signer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    pkg, name = _first_prompt()

    resp = _put(client, agent, pkg, name, "Overridden body for the test.")
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    expected_did = _resolved_operator_did()
    assert payload["signer_did"] == expected_did
    assert payload["signer_did"] not in ("", "did:arc:ui:operator", "did:arc:unknown")

    overlay = agent_dir / "context" / pkg / f"{name}.md"
    sidecar = overlay.with_name(f"{name}.md{_SIDECAR}")
    assert overlay.is_file() and sidecar.is_file()

    # The sidecar carries the RESOLVED signer DID and verifies over the overlay bytes.
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert manifest.signer_did == expected_did
    assert verify_artifact(overlay.read_bytes(), manifest)

    # The list + detail now reflect the override.
    listed = _get(client, f"/api/agents/{agent}/prompts").json()["items"]
    assert any(i["package"] == pkg and i["name"] == name and i["status"] == "overridden" for i in listed)
    detail = _get(client, f"/api/agents/{agent}/prompts/{pkg}/{name}").json()
    assert detail["status"] == "overridden"
    assert detail["effective"] == "Overridden body for the test."
    assert detail["diff"] != ""


def test_put_viewer_is_403_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    pkg, name = _first_prompt()
    resp = _put(client, agent, pkg, name, "nope", token="viewer")
    assert resp.status_code == 403
    assert not (agent_dir / "context" / pkg / f"{name}.md").exists()


def test_put_secret_content_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    pkg, name = _first_prompt()
    resp = _put(client, agent, pkg, name, "api_key = 'AKIA1234567890ABCDEF'")
    assert resp.status_code == 400
    assert not (agent_dir / "context" / pkg / f"{name}.md").exists()


def test_put_unknown_prompt_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    assert _put(client, agent, "arcagent", "does_not_exist", "x").status_code == 404


def test_delete_removes_overlay_and_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    pkg, name = _first_prompt()
    assert _put(client, agent, pkg, name, "body").status_code == 200

    overlay = agent_dir / "context" / pkg / f"{name}.md"
    sidecar = overlay.with_name(f"{name}.md{_SIDECAR}")
    resp = client.delete(
        f"/api/agents/{agent}/prompts/{pkg}/{name}",
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 200
    assert not overlay.exists() and not sidecar.exists()
    assert _get(client, f"/api/agents/{agent}/prompts/{pkg}/{name}").json()["status"] == "stock"


def test_delete_no_override_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    pkg, name = _first_prompt()
    resp = client.delete(
        f"/api/agents/{agent}/prompts/{pkg}/{name}",
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 404


def test_delete_viewer_is_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    pkg, name = _first_prompt()
    resp = client.delete(
        f"/api/agents/{agent}/prompts/{pkg}/{name}",
        headers={"Authorization": "Bearer viewer"},
    )
    assert resp.status_code == 403


# --- Policy gate (COMP-014) ------------------------------------------------


class _DenyPromptWrites:
    """Minimal real policy layer that denies every prompt:write."""

    name = "test-prompt-deny"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.deny(
            layer=self.name,
            rule_id="no-prompt-writes",
            reason="prompt overrides disabled by policy",
            input_hash="test",
            evaluated_at_us=0,
        )


def test_configured_policy_deny_refuses_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    client.app.state.prompt_policy = PolicyPipeline([_DenyPromptWrites()])
    pkg, name = _first_prompt()
    resp = _put(client, agent, pkg, name, "body")
    assert resp.status_code == 403
    assert "policy" in resp.json()["error"].lower()
    assert not (agent_dir / "context" / pkg / f"{name}.md").exists()


def test_no_configured_policy_allows_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    # No app.state.prompt_policy set → configured-gate default is ALLOW.
    pkg, name = _first_prompt()
    assert _put(client, agent, pkg, name, "body").status_code == 200


# --- SigningAuthority seam swap (COMP-011) ---------------------------------


def test_swapping_resolver_changes_signer_with_zero_call_site_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    pkg, name = _first_prompt()

    # Swap ONLY the resolver — no handler edit. A future per-user login is exactly
    # this substitution: a different principal, a different signer DID.
    kp = generate_keypair()
    alt = prompt_signing.SigningIdentity(did="did:arc:custom:tester", seed=kp.private_key)
    monkeypatch.setattr(prompt_signing, "signer_for", lambda request: alt)

    resp = _put(client, agent, pkg, name, "body")
    assert resp.status_code == 200
    assert resp.json()["signer_did"] == "did:arc:custom:tester"

    sidecar = agent_dir / "context" / pkg / f"{name}.md{_SIDECAR}"
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert manifest.signer_did == "did:arc:custom:tester"


# --- Structured rubric editor (arcskill/judge_rubric) ----------------------


def test_get_rubric_returns_structured_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    resp = _get(client, f"/api/agents/{agent}/prompts/{_RUBRIC_PKG}/{_RUBRIC_NAME}/rubric")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stock"
    dims = body["dimensions"]
    # The stock rubric ships these four dimensions in this order.
    assert list(dims) == ["accuracy", "efficiency", "error_handling", "clarity"]
    for dim in dims.values():
        assert isinstance(dim["checklist"], list) and dim["checklist"]
        assert isinstance(dim["anti_inflation"], str) and dim["anti_inflation"]


def test_get_rubric_non_rubric_prompt_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    pkg, name = _first_prompt()
    if (pkg, name) == (_RUBRIC_PKG, _RUBRIC_NAME):
        pytest.skip("first catalog prompt happens to be the rubric")
    resp = _get(client, f"/api/agents/{agent}/prompts/{pkg}/{name}/rubric")
    assert resp.status_code == 404


def test_get_rubric_unknown_agent_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _agent(tmp_path, monkeypatch)
    resp = _get(client, f"/api/agents/nobody/prompts/{_RUBRIC_PKG}/{_RUBRIC_NAME}/rubric")
    assert resp.status_code == 404


def test_put_rubric_round_trips_edit_to_signed_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()

    # Start from the stock structure, edit one checklist row + one calibration.
    current = _get(
        client, f"/api/agents/{agent}/prompts/{_RUBRIC_PKG}/{_RUBRIC_NAME}/rubric"
    ).json()["dimensions"]
    current["accuracy"]["checklist"].append("A brand-new checklist row")
    current["clarity"]["anti_inflation"] = "Recalibrated: a 5 is exceptional."

    resp = _put_rubric(client, agent, {"dimensions": current})
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    expected_did = _resolved_operator_did()
    assert payload["signer_did"] == expected_did
    assert payload["signer_did"] not in ("", "did:arc:ui:operator", "did:arc:unknown")

    overlay = agent_dir / "context" / _RUBRIC_PKG / f"{_RUBRIC_NAME}.md"
    sidecar = overlay.with_name(f"{_RUBRIC_NAME}.md{_SIDECAR}")
    assert overlay.is_file() and sidecar.is_file()

    # The signed overlay verifies over its bytes under the resolved operator DID.
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert manifest.signer_did == expected_did
    assert verify_artifact(overlay.read_bytes(), manifest)

    # Re-reading the rubric reflects the edit, overlay-sourced, order preserved.
    after = _get(
        client, f"/api/agents/{agent}/prompts/{_RUBRIC_PKG}/{_RUBRIC_NAME}/rubric"
    ).json()
    assert after["status"] == "overridden"
    assert list(after["dimensions"]) == ["accuracy", "efficiency", "error_handling", "clarity"]
    assert "A brand-new checklist row" in after["dimensions"]["accuracy"]["checklist"]
    assert after["dimensions"]["clarity"]["anti_inflation"] == "Recalibrated: a 5 is exceptional."

    # The prose prompt-detail surface sees the same override (shared overlay path).
    detail = _get(client, f"/api/agents/{agent}/prompts/{_RUBRIC_PKG}/{_RUBRIC_NAME}").json()
    assert detail["status"] == "overridden"


def test_put_rubric_malformed_body_is_400_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    overlay = agent_dir / "context" / _RUBRIC_PKG / f"{_RUBRIC_NAME}.md"

    # anti_inflation missing on a dimension → structural validation failure.
    bad = {"dimensions": {"accuracy": {"checklist": ["only a checklist"]}}}
    resp = _put_rubric(client, agent, bad)
    assert resp.status_code == 400, resp.text
    assert not overlay.exists()

    # checklist not a list of strings → also 400.
    bad2 = {"dimensions": {"accuracy": {"checklist": "not a list", "anti_inflation": "x"}}}
    assert _put_rubric(client, agent, bad2).status_code == 400
    assert not overlay.exists()


def test_put_rubric_viewer_is_403_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    overlay = agent_dir / "context" / _RUBRIC_PKG / f"{_RUBRIC_NAME}.md"
    body = {"dimensions": {"accuracy": {"checklist": ["x"], "anti_inflation": "y"}}}
    resp = _put_rubric(client, agent, body, token="viewer")
    assert resp.status_code == 403
    assert not overlay.exists()


def test_put_rubric_non_rubric_prompt_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, _ = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    body = {"dimensions": {"accuracy": {"checklist": ["x"], "anti_inflation": "y"}}}
    resp = client.put(
        f"/api/agents/{agent}/prompts/arcskill/not_a_rubric/rubric",
        json=body,
        headers={"Authorization": "Bearer operator"},
    )
    assert resp.status_code == 404


def test_put_rubric_policy_deny_refuses_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, agent, agent_dir = _agent(tmp_path, monkeypatch)
    _mk_operator_key()
    client.app.state.prompt_policy = PolicyPipeline([_DenyPromptWrites()])
    body = {"dimensions": {"accuracy": {"checklist": ["x"], "anti_inflation": "y"}}}
    resp = _put_rubric(client, agent, body)
    assert resp.status_code == 403
    assert "policy" in resp.json()["error"].lower()
    assert not (agent_dir / "context" / _RUBRIC_PKG / f"{_RUBRIC_NAME}.md").exists()
