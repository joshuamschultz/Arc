"""SPEC-066 COMP-010 / REQ-319-REQ-320 — operator signing closes the trust gate.

The loader has refused unsigned capabilities above personal tier since SPEC-033,
but Arc ships no way to actually sign one: ``arc trust approve`` pins a source
hash that the signature floor never lets :class:`~arctrust.TofuLayer` evaluate.
COMP-010 supplies the missing half. ``capability_signing.sign()`` performs one
atomic operator action with three effects — write the detached ``.arcsig``
signature over the artifact bytes, pin the signer's public key as a trusted
capability-verification key in the agent's ``arcagent.toml``, and record the
TOFU pin. ``revoke()`` removes all three.

Every verdict here comes from the REAL :class:`CapabilityLoader` gate
(``require_signature=True``, a pinned key, and a ``TofuLayer`` built from the
agent's own on-disk config), never from a re-implementation, so a verdict in
this file is the verdict a live agent gets.

The trusted key is a SET, not a single value. An operator-signed capability and
an agent-self-signed capability must both be able to pass, so ``sign()``
persists the signer key alongside whatever keys are already pinned rather than
replacing them — see ``test_operator_and_agent_signed_capabilities_coexist``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import (
    ECDSA_P256,
    AuditEvent,
    FileNotaryTransit,
    InProcessSigner,
    TofuLayer,
    VaultSigner,
    generate_keypair,
    hash_source,
    load_validators,
)
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing, capability_signing
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.inventory import collect_agent_capability_inventory
from arcagent.capabilities.reload_models import CapabilityOutcome, ReloadDelta
from arcagent.core.tier import Tier
from arcagent.tools._dynamic_loader import resolve_workspace_import_policy

_PERSONAL_POLICY = resolve_workspace_import_policy(
    "personal", allow_all_imports=False, allow_imports=[]
)

#: Tiers where a signature is the floor — the only tiers this feature changes.
_ABOVE_PERSONAL = [Tier.ENTERPRISE, Tier.FEDERAL]

_OPERATOR_DID = "did:arc:operator:alice"

_TOOL = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='ok', version='1.0.0')\n"
    "async def {fn}() -> str:\n"
    "    return 'ok'\n"
)

_AGENT_TOML = """\
[agent]
name = "signing_probe"

[security]
tier = "federal"

[security.validators]
auto_run_agent_code = false
"""


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    """An agent home with an ``arcagent.toml`` and empty capability roots."""
    root = tmp_path / "agent"
    (root / "capabilities" / "skills").mkdir(parents=True)
    (root / "arcagent.toml").write_text(_AGENT_TOML, encoding="utf-8")
    return root


def _config(agent_root: Path) -> Path:
    return agent_root / "arcagent.toml"


def _write_tool(agent_root: Path, name: str) -> Path:
    """Write an unsigned capability ``.py`` into the per-agent root."""
    path = agent_root / "capabilities" / f"{name}.py"
    path.write_bytes(_TOOL.format(fn=name).encode("utf-8"))
    return path


def _skill_md(name: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "version: 1.0.0\n"
        f"description: does {name}\n"
        f"triggers: [{name}]\n"
        "tools: [reload]\n"
        "---\n"
        "\n## Resources\n\n## Contract\n\n## Knowledge\n\n## Steps\n\n"
        "## Anti Patterns\n\n## Examples\n\n## Validation\n"
    )


def _write_skill(agent_root: Path, name: str) -> Path:
    """Write an unsigned skill folder; returns the gated ``SKILL.md``."""
    folder = agent_root / "capabilities" / "skills" / name
    folder.mkdir(parents=True)
    skill_md = folder / "SKILL.md"
    skill_md.write_bytes(_skill_md(name).encode("utf-8"))
    return skill_md


async def _scan(
    agent_root: Path,
    *,
    tier: Tier,
    trusted_public_key: bytes,
    audit_sink: object | None = None,
) -> ReloadDelta:
    """Run the real loader over the agent's two agent-writable roots.

    The loader pins a SET of keys; these cases each exercise one signer, so they
    hand it a single-element set.
    """
    caps = agent_root / "capabilities"
    loader = CapabilityLoader(
        scan_roots=[("agent", caps), ("agent-skills", caps / "skills")],
        registry=CapabilityRegistry(),
        audit_sink=audit_sink,
        import_policy=_PERSONAL_POLICY,
        tofu=TofuLayer(tier, load_validators(_config(agent_root))),
        require_signature=True,
        trusted_public_keys=(trusted_public_key,),
    )
    return await loader.scan_and_register()


def _outcome(delta: ReloadDelta, name: str) -> CapabilityOutcome:
    for outcome in delta.outcomes:
        if outcome.name == name:
            return outcome
    seen = [o.name for o in delta.outcomes]
    raise AssertionError(f"no loader outcome for {name!r}; loader reported {seen}")


@pytest.mark.parametrize("tier", _ABOVE_PERSONAL)
@pytest.mark.asyncio
async def test_operator_signed_capabilities_pass_the_gate(agent_root: Path, tier: Tier) -> None:
    """An operator signature is the missing half: sign once, and both artifact
    kinds the gate handles load above personal tier."""
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    skill = _write_skill(agent_root, "audit-brief")
    for artifact in (tool, skill):
        capability_signing.sign(
            artifact,
            signer_did=_OPERATOR_DID,
            signer=InProcessSigner(operator.private_key),
            config_path=_config(agent_root),
        )

    delta = await _scan(agent_root, tier=tier, trusted_public_key=operator.public_key)

    assert _outcome(delta, "ledger").status == "loaded"
    assert _outcome(delta, "audit-brief").status == "loaded"
    assert {"ledger", "audit-brief"} <= set(delta.added)


@pytest.mark.parametrize("tier", _ABOVE_PERSONAL)
@pytest.mark.asyncio
async def test_agent_self_signed_capabilities_still_pass(agent_root: Path, tier: Tier) -> None:
    """No regression: an agent signing with its OWN DID key keeps loading.

    Adding operator signing must not narrow the existing self-signed path that
    ``arc agent create`` and the SPEC-033 self-modification tools depend on.
    """
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    tool = _write_tool(agent_root, "ledger")
    skill = _write_skill(agent_root, "audit-brief")
    for artifact in (tool, skill):
        capability_signing.sign(
            artifact,
            signer_did=identity.did,
            signer=InProcessSigner(identity.signing_seed),
            config_path=_config(agent_root),
        )

    delta = await _scan(agent_root, tier=tier, trusted_public_key=identity.public_key)

    assert _outcome(delta, "ledger").status == "loaded"
    assert _outcome(delta, "audit-brief").status == "loaded"
    assert {"ledger", "audit-brief"} <= set(delta.added)


@pytest.mark.parametrize("tier", _ABOVE_PERSONAL)
@pytest.mark.asyncio
async def test_unsigned_capabilities_denied_as_unsigned(agent_root: Path, tier: Tier) -> None:
    """Denial must name the actual cause. ``unsigned`` tells an operator to run
    the signing command; a generic error tells them nothing."""
    operator = generate_keypair()
    _write_tool(agent_root, "ledger")
    _write_skill(agent_root, "audit-brief")

    delta = await _scan(agent_root, tier=tier, trusted_public_key=operator.public_key)

    assert _outcome(delta, "ledger").status == "unsigned"
    assert _outcome(delta, "audit-brief").status == "unsigned"
    assert delta.added == []


@pytest.mark.asyncio
async def test_sign_writes_signature_trusted_key_and_tofu_pin(agent_root: Path) -> None:
    """One call, three durable effects — all three are what makes the gate pass."""
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)

    capability_signing.sign(
        tool,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=config,
    )

    assert artifact_signing.verify_file(
        tool, tool.read_bytes(), trusted_public_key=operator.public_key
    )
    assert operator.public_key.hex() in config.read_text(encoding="utf-8")
    # TOFU keys a tool on its file stem, so the pin must be recorded under it.
    assert [entry.name for entry in load_validators(config).approved] == ["ledger"]


@pytest.mark.asyncio
async def test_sign_pins_a_skill_under_its_folder_name(agent_root: Path) -> None:
    """A skill is gated under its FOLDER name, not its frontmatter name — a pin
    recorded under the wrong key silently leaves the skill gated."""
    operator = generate_keypair()
    skill = _write_skill(agent_root, "audit-brief")
    config = _config(agent_root)

    capability_signing.sign(
        skill,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=config,
    )

    assert [entry.name for entry in load_validators(config).approved] == ["audit-brief"]


@pytest.mark.asyncio
async def test_revoke_removes_signature_trusted_key_and_tofu_pin(agent_root: Path) -> None:
    """Revocation is the exact inverse — the capability returns to gated."""
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)
    capability_signing.sign(
        tool,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=config,
    )

    signed = await _scan(agent_root, tier=Tier.FEDERAL, trusted_public_key=operator.public_key)
    assert _outcome(signed, "ledger").status == "loaded"

    capability_signing.revoke(tool, config_path=config)

    assert not artifact_signing.sidecar_path(tool).exists()
    assert operator.public_key.hex() not in config.read_text(encoding="utf-8")
    assert load_validators(config).approved == ()
    delta = await _scan(agent_root, tier=Tier.FEDERAL, trusted_public_key=operator.public_key)
    assert _outcome(delta, "ledger").status == "unsigned"
    assert "ledger" not in delta.added


@pytest.mark.asyncio
async def test_pinned_key_is_consumed_by_the_real_load_posture(agent_root: Path) -> None:
    """The pin must be WIRED, not merely written.

    Every other test in this file hands the loader the right key directly, so a
    ``sign()`` that wrote a hex string nothing ever reads would pass them all.
    This one goes through the seam a live agent goes through — the inventory
    resolves the trust posture from the agent's own config, and the test never
    supplies the operator key. The probe agent has no DID, so before signing the
    posture resolves no pinned key at all and the gate denies; afterwards the
    ONLY thing that can make it load is the loader consuming the persisted set.
    """
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)

    before = await collect_agent_capability_inventory(config)
    gated = next(item for item in before.items if item.name == "ledger")
    assert gated.status == "unsigned"

    capability_signing.sign(
        tool,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=config,
    )

    after = await collect_agent_capability_inventory(config)
    loaded = next(item for item in after.items if item.name == "ledger")
    assert loaded.status == "loaded", f"pin not consumed at load: {loaded.status_detail}"


@pytest.mark.asyncio
async def test_operator_and_agent_signed_capabilities_coexist(agent_root: Path) -> None:
    """The trusted key is a SET.

    Signing with the operator key must not evict the agent's own key, and vice
    versa. Both keys stay pinned, and each signer's artifact passes the real
    gate under its own key.
    """
    operator = generate_keypair()
    identity = AgentIdentity.generate(org="arc", agent_type="exec")
    config = _config(agent_root)
    operator_tool = _write_tool(agent_root, "ledger")
    agent_tool = _write_tool(agent_root, "notepad")

    capability_signing.sign(
        operator_tool,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=config,
    )
    capability_signing.sign(
        agent_tool,
        signer_did=identity.did,
        signer=InProcessSigner(identity.signing_seed),
        config_path=config,
    )

    persisted = config.read_text(encoding="utf-8")
    assert operator.public_key.hex() in persisted
    assert identity.public_key.hex() in persisted

    under_operator_key = await _scan(
        agent_root, tier=Tier.FEDERAL, trusted_public_key=operator.public_key
    )
    assert _outcome(under_operator_key, "ledger").status == "loaded"

    under_agent_key = await _scan(
        agent_root, tier=Tier.FEDERAL, trusted_public_key=identity.public_key
    )
    assert _outcome(under_agent_key, "notepad").status == "loaded"


# ---------------------------------------------------------------------------
# REQ-323 / COMP-014 — the audit record of a trust mutation
# ---------------------------------------------------------------------------


class _RecordingSink:
    """A REAL :class:`~arctrust.AuditSink` — records what ``emit()`` delivers.

    Deliberately not a patch of ``arctrust.audit.emit``: patching the emitter
    proves only that a call was made, while a sink proves the event travelled
    the real emission path and arrived in the shape a WORM chain would store.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def only(self, action: str) -> AuditEvent:
        matched = [event for event in self.events if event.action == action]
        assert len(matched) == 1, f"expected exactly one {action}, got {[e.action for e in matched]}"
        return matched[0]


class _RecordingLoaderSink:
    """The loader's audit seam — ``emit(event)``, not the arctrust ``write``."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_sign_and_revoke_emit_through_a_real_sink(agent_root: Path) -> None:
    """One operator action, one audit record — naming operator, path, and hash.

    The hash is asserted against the TOFU pin the SAME call wrote, because an
    audit line whose hash disagrees with the pin line cannot be used to answer
    "which bytes did the operator actually approve?".
    """
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)
    sink = _RecordingSink()

    capability_signing.sign(
        tool,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=config,
        audit_sink=sink,
    )

    pinned_hash = load_validators(config).approved[0].hash
    signed = sink.only("capability.signed")
    assert signed.actor_did == _OPERATOR_DID
    assert signed.target == str(tool)
    assert signed.outcome == "signed"
    assert signed.payload_hash == pinned_hash == hash_source(tool.read_text(encoding="utf-8"))

    capability_signing.revoke(
        tool, config_path=config, operator_did=_OPERATOR_DID, audit_sink=sink
    )

    revoked = sink.only("capability.signature_revoked")
    assert revoked.actor_did == _OPERATOR_DID
    assert revoked.target == str(tool)
    assert revoked.outcome == "revoked"
    assert revoked.payload_hash == pinned_hash


@pytest.mark.asyncio
async def test_audit_records_carry_no_key_material(agent_root: Path) -> None:
    """A trust record proves WHICH bytes were approved, never with what secret.

    Serialized whole, because a seed leaking through ``extra`` or a stringified
    field would be invisible to a per-field assertion.
    """
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    sink = _RecordingSink()

    capability_signing.sign(
        tool,
        signer_did=_OPERATOR_DID,
        signer=InProcessSigner(operator.private_key),
        config_path=_config(agent_root),
        audit_sink=sink,
    )
    capability_signing.revoke(
        tool, config_path=_config(agent_root), operator_did=_OPERATOR_DID, audit_sink=sink
    )

    for event in sink.events:
        serialized = event.model_dump_json()
        assert operator.private_key.hex() not in serialized
        assert str(operator.private_key) not in serialized


@pytest.mark.asyncio
async def test_revocation_of_a_deleted_artifact_still_records(agent_root: Path) -> None:
    """Revocation tolerates a missing artifact; the audit must not undo that.

    Reading the artifact for its hash is the one new file read ``revoke`` does —
    an unreadable one costs the record its hash, never the revocation.
    """
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)
    capability_signing.sign(
        tool, signer_did=_OPERATOR_DID, signer=InProcessSigner(operator.private_key), config_path=config
    )
    sink = _RecordingSink()
    tool.unlink()

    capability_signing.revoke(
        tool, config_path=config, operator_did=_OPERATOR_DID, audit_sink=sink
    )

    revoked = sink.only("capability.signature_revoked")
    assert revoked.payload_hash is None
    assert load_validators(config).approved == ()


def _refusal(sink: _RecordingLoaderSink, action: str) -> AuditEvent:
    matched = [event for event in sink.events if event.action == action]
    assert len(matched) == 1, f"expected one {action}; loader recorded {[e.action for e in sink.events]}"
    return matched[0]


@pytest.mark.asyncio
async def test_unsigned_refusal_keeps_its_existing_audit_shape(agent_root: Path) -> None:
    """The signature-floor refusal is the loader's record; T-954 leaves it alone.

    A refusal has no operator — the loader is the actor — so it names the
    artifact path and the reason only. Locked here so a later change to the
    signing events cannot quietly reshape the refusal beside them.
    """
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    sink = _RecordingLoaderSink()

    await _scan(
        agent_root, tier=Tier.FEDERAL, trusted_public_key=operator.public_key, audit_sink=sink
    )

    deny = _refusal(sink, "capability.signature")
    assert deny.actor_did == "did:arc:capability-loader"
    assert deny.target == str(tool)
    assert deny.outcome == "error"
    assert deny.extra == {"path": str(tool), "reason": "missing or invalid signature"}


@pytest.mark.asyncio
async def test_drift_refusal_keeps_its_existing_audit_shape(agent_root: Path) -> None:
    """``capability:deny`` — the TOFU drift refusal — is unchanged too.

    Reached by re-signing edited bytes WITHOUT re-approving them: the signature
    verifies, so the floor passes and the stale hash pin is what refuses. That is
    the only way to exercise the deny branch rather than the signature branch.
    """
    operator = generate_keypair()
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)
    capability_signing.sign(
        tool, signer_did=_OPERATOR_DID, signer=InProcessSigner(operator.private_key), config_path=config
    )
    drifted = tool.read_bytes() + b"\n# edited after approval\n"
    tool.write_bytes(drifted)
    artifact_signing.write_signature(
        tool, drifted, signer_did=_OPERATOR_DID, private_key=operator.private_key
    )
    sink = _RecordingLoaderSink()

    await _scan(
        agent_root, tier=Tier.FEDERAL, trusted_public_key=operator.public_key, audit_sink=sink
    )

    deny = _refusal(sink, "capability.deny")
    assert deny.actor_did == "did:arc:capability-loader"
    assert deny.target == str(tool)
    assert deny.outcome == "error"
    assert deny.extra == {"path": str(tool), "reason": "tofu decision deny"}


# ---------------------------------------------------------------------------
# D-066-2 — signing works at EVERY tier, through the tier's own key custody
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", _ABOVE_PERSONAL)
@pytest.mark.asyncio
async def test_ecdsa_p256_signed_capabilities_pass_the_gate(agent_root: Path, tier: Tier) -> None:
    """Federal signs with ECDSA-P256 — the REAL loader gate must accept it.

    Before D-066-2 the verifier hardcoded Ed25519, so the one algorithm the
    federal tier mandates could neither be produced nor verified: signing was
    impossible on exactly the deployment this gate exists for.
    """
    seed = generate_keypair().private_key
    signer = InProcessSigner(seed, ECDSA_P256)
    tool = _write_tool(agent_root, "ledger")
    skill = _write_skill(agent_root, "audit-brief")
    for artifact in (tool, skill):
        capability_signing.sign(
            artifact,
            signer_did=_OPERATOR_DID,
            signer=signer,
            config_path=_config(agent_root),
        )

    delta = await _scan(agent_root, tier=tier, trusted_public_key=signer.public_key)

    assert _outcome(delta, "ledger").status == "loaded"
    assert _outcome(delta, "audit-brief").status == "loaded"
    assert {"ledger", "audit-brief"} <= set(delta.added)


@pytest.mark.asyncio
async def test_ecdsa_signing_pins_the_ecdsa_key_not_an_ed25519_one(agent_root: Path) -> None:
    """The pinned trust anchor is the SIGNER's key, at the signer's algorithm.

    The same 32 bytes derive a different public key under each primitive, so
    pinning the Ed25519 one would leave a federal capability verifiable by a key
    that never signed it — and gated by a pin its own signature cannot match.
    """
    seed = generate_keypair().private_key
    ed25519_key = generate_keypair().public_key  # not the signer's key
    signer = InProcessSigner(seed, ECDSA_P256)
    tool = _write_tool(agent_root, "ledger")
    config = _config(agent_root)

    capability_signing.sign(tool, signer_did=_OPERATOR_DID, signer=signer, config_path=config)

    trusted = load_validators(config).trusted_keys
    assert signer.public_key.hex() in trusted
    assert ed25519_key.hex() not in trusted
    manifest = artifact_signing.load_signature(tool)
    assert manifest is not None
    assert manifest.algorithm == ECDSA_P256
    assert manifest.public_key == signer.public_key.hex()


@pytest.mark.asyncio
async def test_vault_transit_signed_capability_passes_the_gate(agent_root: Path) -> None:
    """The federal custody path end to end: the seed never enters this process.

    Uses the real :class:`FileNotaryTransit` — the out-of-process signer a
    ``custody = "vault_transit"`` deployment actually drives. A fake transit
    would prove nothing about the path federal runs.
    """
    seed = generate_keypair().private_key
    keystore = agent_root / "notary"
    FileNotaryTransit.provision(keystore, "operator", seed, algorithm=ECDSA_P256)
    signer = VaultSigner(
        FileNotaryTransit(keystore, algorithm=ECDSA_P256), "operator", ECDSA_P256
    )
    tool = _write_tool(agent_root, "ledger")

    capability_signing.sign(
        tool, signer_did=_OPERATOR_DID, signer=signer, config_path=_config(agent_root)
    )

    delta = await _scan(agent_root, tier=Tier.FEDERAL, trusted_public_key=signer.public_key)
    assert _outcome(delta, "ledger").status == "loaded"


@pytest.mark.asyncio
async def test_relabelling_a_signed_sidecar_denies_at_the_loader(agent_root: Path) -> None:
    """Algorithm confusion is refused by the gate, not just by the primitive.

    The signature bytes, the key, and the TOFU pin all stay valid; only the
    sidecar's ``algorithm`` is swapped. If the loader accepted that, an attacker
    with write access to a sidecar could choose the verifier — or none at all.
    """
    seed = generate_keypair().private_key
    signer = InProcessSigner(seed, ECDSA_P256)
    tool = _write_tool(agent_root, "ledger")
    capability_signing.sign(
        tool, signer_did=_OPERATOR_DID, signer=signer, config_path=_config(agent_root)
    )
    sidecar = artifact_signing.sidecar_path(tool)
    manifest = artifact_signing.load_signature(tool)
    assert manifest is not None
    sidecar.write_text(
        manifest.model_copy(update={"algorithm": "ed25519"}).to_json(), encoding="utf-8"
    )

    delta = await _scan(agent_root, tier=Tier.FEDERAL, trusted_public_key=signer.public_key)

    assert _outcome(delta, "ledger").status == "unsigned"
    assert delta.added == []
