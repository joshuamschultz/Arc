"""``/api/trust/*`` — operator-gated capability-trust surface (SPEC-021, SPEC-066).

GET lists gated capabilities across the roster (any role) via the arcagent
inventory seam; POST approve/disapprove is operator-only and SIGNS the artifact
with the deployment operator key (SPEC-066 COMP-010/COMP-012) rather than
pinning a source hash the signature floor never lets the loader reach. A viewer
is refused and nothing is written; an operator flips a gated capability to
``loaded`` and can revoke it again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcagent.capabilities import artifact_signing
from arcgateway import team_roster
from arctrust import OperatorKey, arc_home, default_operator_key_path
from arctrust.identity import AgentIdentity
from arctrust.validators import load_validators
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditEvent
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


class _SpyAudit:
    """Captures ``audit_event`` calls so a test can assert the recorded outcome."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: Any, details: dict[str, Any]) -> None:
        name = event_type.value if isinstance(event_type, UIAuditEvent) else event_type
        self.events.append((name, details))

    def outcomes_for(self, operation: str) -> list[str]:
        return [d["outcome"] for _, d in self.events if d.get("operation") == operation]


def _make_client(team_root: Path) -> TestClient:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=trust_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = _SpyAudit()
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return TestClient(app)


def _audit(client: TestClient) -> _SpyAudit:
    spy = client.app.state.audit  # type: ignore[attr-defined]  # reason: Starlette state is untyped
    assert isinstance(spy, _SpyAudit)
    return spy


def _skill_md(team_root: Path, name: str) -> Path:
    """The gated artifact ``_build_agent`` wrote for agent ``name``."""
    return team_root / name / "workspace" / "capabilities" / "skills" / "reporter" / "SKILL.md"


def _pinned_keys(team_root: Path, name: str) -> tuple[str, ...]:
    """Capability-verification keys trusted by agent ``name``'s config."""
    return load_validators(team_root / name / "arcagent.toml").trusted_keys


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
        "agent_id",
        "agent_label",
        "name",
        "kind",
        "status",
        "path",
        "hash",
        "detail",
    }


def test_approve_requires_operator_and_signs_nothing(tmp_path: Path) -> None:
    """A viewer is refused BEFORE any signing side effect reaches the disk."""
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_VIEWER, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "operator_role_required"

    assert not artifact_signing.sidecar_path(_skill_md(team_root, "olivia")).exists()
    assert _pinned_keys(team_root, "olivia") == ()
    assert _audit(client).outcomes_for("trust.approve") == ["denied"]


def test_operator_approve_signs_the_artifact(tmp_path: Path) -> None:
    """Approval writes the sidecar and pins the operator key — not a hash alone.

    The capability starts UNSIGNED at enterprise tier, where the loader's
    signature floor refuses it. Only a real signature can flip it to ``loaded``,
    so ``status == "loaded"`` here is unforgeable evidence that the route signed.
    """
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "loaded"

    skill_md = _skill_md(team_root, "olivia")
    assert artifact_signing.sidecar_path(skill_md).exists()
    operator_public = OperatorKey.load(default_operator_key_path()).public_key
    assert operator_public.hex() in _pinned_keys(team_root, "olivia")
    assert artifact_signing.verify_file(
        skill_md, skill_md.read_bytes(), trusted_public_key=operator_public
    )
    assert _audit(client).outcomes_for("trust.approve") == ["applied"]


def test_disapprove_removes_the_sidecar_and_unpins_the_key(tmp_path: Path) -> None:
    """Revocation is the exact inverse: signature gone, key unpinned, gated again."""
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    resp = client.post(
        "/api/trust/disapprove", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 200

    assert not artifact_signing.sidecar_path(_skill_md(team_root, "olivia")).exists()
    assert _pinned_keys(team_root, "olivia") == ()
    gated = client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
    assert "reporter" in {it["name"] for it in gated}


def test_missing_operator_key_is_500_and_signs_nothing(tmp_path: Path) -> None:
    """No pinned operator is no operator: fail closed, audit denied, write nothing."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 500
    assert resp.json()["error"].startswith("operator_key_unavailable")

    assert not artifact_signing.sidecar_path(_skill_md(team_root, "olivia")).exists()
    assert _pinned_keys(team_root, "olivia") == ()
    assert _audit(client).outcomes_for("trust.approve") == ["denied"]


def test_vault_transit_custody_refuses_to_sign(tmp_path: Path) -> None:
    """Under vault-transit custody no seed exists in-process — refuse, never substitute.

    A stale on-disk operator key is present here precisely because that is the
    dangerous case: signing with it would mint an authority the deployment moved
    to a vault, so the route must refuse rather than reach for whatever key it
    can find.
    """
    _bootstrap_operator_key(tmp_path)
    (arc_home() / "arcagent.toml").write_text(
        '[security]\ncustody = "vault_transit"\n', encoding="utf-8"
    )
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 500
    # The refusal must be actionable: name the cause AND a way out, or the
    # operator cannot tell it apart from a broken deployment.
    error = resp.json()["error"]
    assert error.startswith("operator_key_not_in_process: custody=vault_transit")
    assert 'custody = "in_process"' in error

    assert not artifact_signing.sidecar_path(_skill_md(team_root, "olivia")).exists()
    assert _pinned_keys(team_root, "olivia") == ()
    assert _audit(client).outcomes_for("trust.approve") == ["denied"]


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
