"""``/api/trust/*`` — operator-gated capability-trust surface (SPEC-021, SPEC-066).

GET lists gated capabilities across the roster (any role) via the arcagent
inventory seam; POST approve/disapprove is operator-only and SIGNS the artifact
with the deployment operator key (SPEC-066 COMP-010/COMP-012) rather than
pinning a source hash the signature floor never lets the loader reach. A viewer
is refused and nothing is written; an operator flips a gated capability to
``loaded`` and can revoke it again.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcagent.capabilities import artifact_signing
from arcgateway import team_roster
from arctrust import OperatorKey, arc_home, default_operator_key_path, generate_keypair
from arctrust.audit import verify_chain
from arctrust.identity import AgentIdentity
from arctrust.signer import ECDSA_P256, FileNotaryTransit, Signer, VaultSigner
from arctrust.validators import load_validators
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import MutationWormWriter, UIAuditEvent, build_mutation_worm_writer
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


def _make_client(team_root: Path, *, worm: MutationWormWriter | None = None) -> TestClient:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=trust_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = _SpyAudit()
    # The signed chain this server holds. Absent by default (a bare app degrades
    # to log+OTel); supplied by the REQ-323 cases that read records back off it.
    app.state.audit_worm = worm
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


def _agent_did(team_root: Path, name: str) -> str:
    """Agent ``name``'s own DID — the signer of a self-signed artifact."""
    config = (team_root / name / "arcagent.toml").read_text(encoding="utf-8")
    return str(tomllib.loads(config)["identity"]["did"])


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
        # arcui's own enrichment: who signed the artifact on disk right now.
        # This agent's skill is self-signed with its own DID, which is exactly
        # the case ``status`` cannot distinguish from an operator signature.
        "signer_did",
    }
    assert item["signer_did"].startswith("did:arc:")


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


def _use_vault_transit_custody(tmp_path: Path) -> Signer:
    """Put the deployment on the real federal posture: ECDSA-P256 out-of-process.

    ``tier = "federal"`` is what selects ``custody = "vault_transit"`` and
    ``signing_algorithm = "ecdsa-p256"``; writing them by hand would test a
    posture no deployment produces. The notary keystore is provisioned with the
    REAL :class:`FileNotaryTransit`, so the route signs by reference exactly as
    a federal box does. Returns the signer it is expected to sign with.
    """
    keystore = arc_home() / "notary"
    FileNotaryTransit.provision(
        keystore, "operator", generate_keypair().private_key, algorithm=ECDSA_P256
    )
    (arc_home() / "arcagent.toml").write_text(
        f'[security]\ntier = "federal"\nnotary_keystore = "{keystore}"\n', encoding="utf-8"
    )
    return VaultSigner(FileNotaryTransit(keystore, algorithm=ECDSA_P256), "operator", ECDSA_P256)


def test_vault_transit_custody_signs_through_the_notary(tmp_path: Path) -> None:
    """Approval must WORK under vault custody — the tier this gate exists for.

    The seed never enters this process; the route signs by reference and pins
    the notary's own ECDSA-P256 verify key. An on-disk operator key is present
    precisely because it must NOT be the one used: signing with it would mint an
    authority the deployment deliberately moved to a vault.
    """
    _bootstrap_operator_key(tmp_path)
    signer = _use_vault_transit_custody(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "loaded"
    skill = _skill_md(team_root, "olivia")
    manifest = artifact_signing.load_signature(skill)
    assert manifest is not None
    assert manifest.algorithm == ECDSA_P256
    assert manifest.public_key == signer.public_key.hex()
    on_disk = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    assert manifest.public_key != on_disk.public_key.hex()
    assert _pinned_keys(team_root, "olivia") == (signer.public_key.hex(),)
    assert _audit(client).outcomes_for("trust.approve") == ["applied"]


def test_vault_transit_without_a_provisioned_notary_refuses(tmp_path: Path) -> None:
    """A transit that cannot serve the operator key must never fall back.

    The on-disk key is right there; reaching for it would defeat the custody
    move. Fail closed, audit the denial, and write nothing.
    """
    _bootstrap_operator_key(tmp_path)
    (arc_home() / "arcagent.toml").write_text(
        f'[security]\ntier = "federal"\nnotary_keystore = "{arc_home() / "absent"}"\n',
        encoding="utf-8",
    )
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


# ── Reaching capabilities that already load (pre-sign / re-sign) ─────────────
#
# The scenario every test below is built on: personal tier, artifact signed with
# the AGENT's own DID. The loader is happy — status ``loaded`` — so before
# SPEC-066's arcui half none of it was reachable from the dashboard: the gated
# listing filtered it out and approve resolved its target from that same
# filtered listing. An operator who hand-edited that skill, or who wanted the
# OPERATOR key on it before promoting the agent to enterprise, had no route.


def test_gated_listing_hides_loaded_capabilities_by_default(tmp_path: Path) -> None:
    """The default view stays the attention queue, not an inventory dump."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)

    gated = client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
    assert gated == []


def test_include_loaded_reveals_the_loaded_capability_and_its_signer(tmp_path: Path) -> None:
    """Opt in and the loaded rows appear, each naming who actually signed it.

    ``status`` cannot answer "who signed this": this artifact is ``loaded`` and
    signed by the AGENT, which is precisely the case an operator must be able to
    tell apart from one they signed themselves.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)

    gated = client.get("/api/trust/gated?include_loaded=1", headers=_VIEWER).json()["gated"]
    item = next(it for it in gated if it["name"] == "reporter")
    assert item["status"] == "loaded"
    assert item["signer_did"] == _agent_did(team_root, "olivia")
    # The builtin tools ride along too — which is exactly why this is opt-in.
    assert "write" in {it["name"] for it in gated}


def test_include_loaded_is_off_unless_the_flag_is_truthy(tmp_path: Path) -> None:
    """A stray or negative value must not silently widen the default view."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)

    for query in ("", "?include_loaded=0", "?include_loaded=false", "?include_loaded=maybe"):
        assert client.get(f"/api/trust/gated{query}", headers=_VIEWER).json()["gated"] == []


def test_source_is_readable_for_a_loaded_capability(tmp_path: Path) -> None:
    """Without this the re-sign gate could never unlock.

    The approve control unlocks only while the rendered source hash equals the
    row hash. A loaded capability whose source 404'd would be permanently
    locked out of re-signing — and an operator with a real need would go find
    an ungated way to do it.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)

    row = next(
        it
        for it in client.get("/api/trust/gated?include_loaded=1", headers=_VIEWER).json()["gated"]
        if it["name"] == "reporter"
    )
    resp = client.get(
        "/api/trust/source", params={"agent_id": "olivia", "name": "reporter"}, headers=_VIEWER
    )
    assert resp.status_code == 200
    assert resp.json()["hash"] == row["hash"]
    assert "name: reporter" in resp.json()["source"]


def test_operator_can_sign_a_capability_that_already_loads(tmp_path: Path) -> None:
    """The gap SPEC-066's arcui half closes: re-signing an agent-signed artifact.

    Nothing about this capability is broken — it loads. The operator is taking
    custody of it, so the assertion that matters is that the sidecar's signer
    FLIPS from the agent's DID to the operator's, and the artifact now verifies
    under the operator key.
    """
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="personal", sign=True)
    client = _make_client(team_root)
    agent_did = _agent_did(team_root, "olivia")

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "loaded"
    assert body["resigned"] is True
    assert body["signer_did"] != agent_did

    skill_md = _skill_md(team_root, "olivia")
    operator_public = OperatorKey.load(default_operator_key_path()).public_key
    assert artifact_signing.verify_file(
        skill_md, skill_md.read_bytes(), trusted_public_key=operator_public
    )
    assert operator_public.hex() in _pinned_keys(team_root, "olivia")
    assert _audit(client).outcomes_for("trust.approve") == ["applied"]


def test_a_first_signature_is_reported_as_a_first_signature(tmp_path: Path) -> None:
    """``resigned`` must distinguish the two, or the UI cannot warn about either."""
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    first = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert first.json()["resigned"] is False

    second = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert second.json()["resigned"] is True


def test_resigning_pins_the_edited_bytes_not_the_approved_ones(tmp_path: Path) -> None:
    """A re-sign signs what is on disk NOW — the whole point after a hand edit.

    The edit invalidates the old signature and the old pin, so the capability is
    gated again. Re-signing must move BOTH forward: a route that re-wrote the
    sidecar without re-pinning would leave it gated and look like a no-op.
    """
    _bootstrap_operator_key(tmp_path)
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)
    client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )

    skill_md = _skill_md(team_root, "olivia")
    skill_md.write_bytes(skill_md.read_bytes() + b"\n## Extra\n\nhand edited\n")
    edited = client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
    assert next(it for it in edited if it["name"] == "reporter")["status"] != "loaded"

    resp = client.post(
        "/api/trust/approve", headers=_OPERATOR, json={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "loaded"
    assert resp.json()["resigned"] is True
    operator_public = OperatorKey.load(default_operator_key_path()).public_key
    assert artifact_signing.verify_file(
        skill_md, skill_md.read_bytes(), trusted_public_key=operator_public
    )


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


def test_viewer_can_read_capability_source(tmp_path: Path) -> None:
    """Reading is a viewer right — reviewing the artifact must not need the operator token.

    The returned text is the artifact's exact bytes: the operator is judging
    what the loader will hash, not a re-rendering of it.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.get(
        "/api/trust/source", headers=_VIEWER, params={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 200
    body = resp.json()
    skill_md = _skill_md(team_root, "olivia")
    assert body["source"] == skill_md.read_bytes().decode("utf-8")
    assert body["path"] == str(skill_md)


def test_source_hash_matches_the_gated_row(tmp_path: Path) -> None:
    """The source carries the same hash the row shows — that pairing IS the gate.

    The UI enables approve only while the rendered source's hash equals the
    listed row's hash, so a drift between the two must be observable here. If
    these could disagree for identical bytes the gate would refuse every
    honest review.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    row = next(
        it
        for it in client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
        if it["name"] == "reporter"
    )
    body = client.get(
        "/api/trust/source", headers=_VIEWER, params={"agent_id": "olivia", "name": "reporter"}
    ).json()
    assert body["hash"] == row["hash"] != ""

    # Rewriting the artifact must move both together, or a stale review would
    # keep approving bytes nobody read.
    _skill_md(team_root, "olivia").write_text(
        _VALID_SKILL.format(name="reporter") + "\nedited\n", encoding="utf-8"
    )
    after_row = next(
        it
        for it in client.get("/api/trust/gated", headers=_VIEWER).json()["gated"]
        if it["name"] == "reporter"
    )
    after = client.get(
        "/api/trust/source", headers=_VIEWER, params={"agent_id": "olivia", "name": "reporter"}
    ).json()
    assert after["hash"] == after_row["hash"] != row["hash"]


def test_source_requires_authentication(tmp_path: Path) -> None:
    """Artifact text is executable code — it is never anonymously readable."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.get("/api/trust/source", params={"agent_id": "olivia", "name": "reporter"})
    assert resp.status_code == 401


def test_source_unknown_agent_is_404(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    team_root.mkdir()
    client = _make_client(team_root)

    resp = client.get(
        "/api/trust/source", headers=_VIEWER, params={"agent_id": "ghost", "name": "reporter"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "agent_not_found"


def test_source_unknown_capability_is_404(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.get(
        "/api/trust/source", headers=_VIEWER, params={"agent_id": "olivia", "name": "ghost"}
    )
    assert resp.status_code == 404


def test_source_requires_agent_id_and_name(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root)

    resp = client.get("/api/trust/source", headers=_VIEWER, params={"agent_id": "olivia"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "agent_id and name are required"


def test_source_of_an_unreadable_artifact_is_404(tmp_path: Path) -> None:
    """An artifact that cannot even be scanned is unreviewable, so unapprovable.

    Undecodable bytes make ``arcagent``'s skill validator raise, which would
    otherwise surface as an unhandled 500 — a shape the frontend reads as a
    transport failure rather than a refusal. The gate must fail CLOSED and say
    so: no source rendered means the approve action never unlocks.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    _skill_md(team_root, "olivia").write_bytes(b"\xff\xfe not utf-8")
    client = _make_client(team_root)

    resp = client.get(
        "/api/trust/source", headers=_VIEWER, params={"agent_id": "olivia", "name": "reporter"}
    )
    assert resp.status_code == 404
    assert "cannot read capability source" in resp.json()["error"]


# ---------------------------------------------------------------------------
# REQ-323 / COMP-014 — the capability-level record beside the HTTP mutation
# ---------------------------------------------------------------------------


@pytest.fixture
def worm(tmp_path: Path) -> Iterator[MutationWormWriter]:
    """The REAL operator-signed chain this server would hold in production.

    A ``WormSink`` keeps an exclusive flock for its lifetime, so the fixture
    closes it — leaving it open would lock out every later writer in the run.
    """
    _bootstrap_operator_key(tmp_path)
    writer = build_mutation_worm_writer(tmp_path / "ui-data")
    assert writer is not None, "operator key was bootstrapped; the chain must open"
    yield writer
    writer.sink.close()


def _chain_path(tmp_path: Path) -> Path:
    return tmp_path / "ui-data" / "worm" / "audit-chain-arcui.jsonl"


def _chain_event(tmp_path: Path, action: str) -> dict[str, Any]:
    """One event read back OFF DISK — proof the record reached a real sink."""
    lines = _chain_path(tmp_path).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line)["event"] for line in lines if line]
    matched = [event for event in events if event["action"] == action]
    assert len(matched) == 1, f"expected one {action}; chain has {[e['action'] for e in events]}"
    return matched[0]


def test_approve_and_disapprove_record_on_the_servers_chain(
    tmp_path: Path, worm: MutationWormWriter
) -> None:
    """REQ-323 — the browser path records the same event the CLI path does.

    Read back off the signed chain rather than from a captured sink, so an
    emission that reaches nothing fails here. ``payload_hash`` is checked against
    the TOFU pin the same request wrote: an audit line that disagrees with the
    pin cannot answer which bytes the operator approved.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root, worm=worm)
    body = {"agent_id": "olivia", "name": "reporter"}
    skill = _skill_md(team_root, "olivia")

    assert client.post("/api/trust/approve", json=body, headers=_OPERATOR).status_code == 200

    pinned_hash = load_validators(team_root / "olivia" / "arcagent.toml").approved[0].hash
    signed = _chain_event(tmp_path, "capability.signed")
    assert signed["actor_did"] == worm.operator_did
    assert signed["target"] == str(skill)
    assert signed["outcome"] == "signed"
    assert signed["payload_hash"] == pinned_hash

    assert client.post("/api/trust/disapprove", json=body, headers=_OPERATOR).status_code == 200

    revoked = _chain_event(tmp_path, "capability.signature_revoked")
    assert revoked["actor_did"] == worm.operator_did
    assert revoked["target"] == str(skill)
    assert revoked["outcome"] == "revoked"
    assert revoked["payload_hash"] == pinned_hash


def test_the_capability_record_joins_the_mutation_record_on_one_chain(
    tmp_path: Path, worm: MutationWormWriter
) -> None:
    """One chain, not two: the HTTP mutation and the capability event share it.

    The route already audits ``trust.approve``; the capability event is the new
    one. Opening a second chain for it would put the two halves of one action in
    two files, and a ``WormSink``'s exclusive flock would make that fail anyway.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root, worm=worm)

    client.post(
        "/api/trust/approve", json={"agent_id": "olivia", "name": "reporter"}, headers=_OPERATOR
    )

    lines = _chain_path(tmp_path).read_text(encoding="utf-8").splitlines()
    actions = [json.loads(line)["event"]["action"] for line in lines if line]
    assert actions == ["capability.signed", "trust.approve"]


def test_the_servers_chain_verifies_and_holds_no_key_material(
    tmp_path: Path, worm: MutationWormWriter
) -> None:
    """The chain verifies under the operator key and leaks no secret.

    ``verify_chain`` is what separates a real sink from a list of dicts: it only
    passes if every record was Ed25519-signed and hash-linked on the way to disk.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    _build_agent(team_root, "olivia", tier="enterprise", sign=False)
    client = _make_client(team_root, worm=worm)
    body = {"agent_id": "olivia", "name": "reporter"}

    client.post("/api/trust/approve", json=body, headers=_OPERATOR)
    client.post("/api/trust/disapprove", json=body, headers=_OPERATOR)

    key = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    assert verify_chain(_chain_path(tmp_path), key.public_key)
    raw = _chain_path(tmp_path).read_text(encoding="utf-8")
    assert key.seed.hex() not in raw
    assert key.public_key.hex() not in raw
