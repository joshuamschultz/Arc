"""Identity authentication for tool calls — signature + DID binding + admission.

The invariant under test (the "SSH-key model"): nothing runs unless the call
is signed by an agent that holds the private key for the DID it claims, and —
at enterprise/federal — that agent is explicitly admitted (deny-by-default).

These tests prove:
- A validly self-signed call verifies (personal admits self-signed agents).
- An unsigned call is denied at every tier.
- A tampered call (arguments changed after signing) is denied.
- Impersonation is impossible: agent A cannot produce a call that verifies as
  agent B, because A holds neither B's key nor a pubkey matching B's DID.
- enterprise/federal deny unknown agents by default even when self-signed.
"""

from __future__ import annotations

import pytest

from arctrust.classification import Classification
from arctrust.identity import AgentIdentity, did_matches_pubkey
from arctrust.policy import (
    ClearanceContext,
    IdentityLayer,
    PolicyContext,
    ProviderUsage,
    ToolCall,
    ToolRuntimeStatus,
    build_pipeline,
    sign_call,
    verify_call,
)


def _unsigned_call(agent_did: str, tool_name: str = "read") -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments={"path": "/etc/passwd"},
        agent_did=agent_did,
        session_id="s1",
        classification="unclassified",
    )


def _ctx(tier: str) -> PolicyContext:
    # Inject clean provider/runtime/clearance state so the now-real
    # Provider/Sandbox/Classification layers (fail-closed above personal on
    # missing state; federal forces classification enforcement) pass through and
    # the identity/admission behavior under test is what decides the outcome.
    return PolicyContext(
        tier=tier,  # type: ignore[arg-type]
        policy_version="v1",
        bundle_age_seconds=0.0,
        provider_usage=ProviderUsage(
            provider="anthropic", tokens_used=0, cost_used=0.0, requests_in_window=0
        ),
        tool_runtime=ToolRuntimeStatus(
            verified=True, required_isolation="host", available_isolation="host"
        ),
        clearance=ClearanceContext(
            caller_clearance=Classification.UNCLASSIFIED,
            resource_classification=Classification.UNCLASSIFIED,
        ),
    )


# --- DID <-> pubkey binding -------------------------------------------------


def test_did_matches_its_own_pubkey() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    assert did_matches_pubkey(ident.did, ident.public_key) is True


def test_did_does_not_match_a_different_pubkey() -> None:
    a = AgentIdentity.generate(org="default", agent_type="executor")
    b = AgentIdentity.generate(org="default", agent_type="executor")
    assert did_matches_pubkey(a.did, b.public_key) is False


# --- sign_call / verify_call ------------------------------------------------


def test_self_signed_call_verifies() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    signed = sign_call(_unsigned_call(ident.did), ident)
    assert signed.signature is not None and signed.public_key is not None
    assert verify_call(signed) is True


def test_unsigned_call_does_not_verify() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    assert verify_call(_unsigned_call(ident.did)) is False


def test_tampered_arguments_break_verification() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    signed = sign_call(_unsigned_call(ident.did), ident)
    tampered = signed.model_copy(update={"arguments": {"path": "/etc/shadow"}})
    assert verify_call(tampered) is False


def test_sign_call_rebinds_did_to_signer() -> None:
    """The helper binds the call to the SIGNER — you can't sign as someone else."""
    a = AgentIdentity.generate(org="default", agent_type="executor")
    b = AgentIdentity.generate(org="default", agent_type="planner")
    signed = sign_call(_unsigned_call(b.did), a)  # ask to claim B, sign with A
    assert signed.agent_did == a.did  # rebound to A, not B
    assert verify_call(signed) is True


def test_impersonation_with_own_key_but_victims_did_fails() -> None:
    """Hand-crafted forgery: A signs but stamps B's DID — fingerprint mismatch → reject."""
    a = AgentIdentity.generate(org="default", agent_type="executor")
    b = AgentIdentity.generate(org="default", agent_type="planner")
    # Attacker bypasses sign_call's rebinding and hand-builds the call: claims
    # B's DID, but presents A's pubkey and an A-signature over the content.
    body = _unsigned_call(b.did)
    forged = body.model_copy(
        update={
            "public_key": a.public_key,
            "signature": a.sign(body.signing_bytes()),
        }
    )
    assert forged.agent_did == b.did
    assert verify_call(forged) is False


def test_impersonation_with_victim_did_and_victim_pubkey_but_no_key_fails() -> None:
    """A stamps B's DID and B's pubkey but cannot sign for B → reject."""
    a = AgentIdentity.generate(org="default", agent_type="executor")
    b = AgentIdentity.generate(org="default", agent_type="planner")
    signed_by_a = sign_call(_unsigned_call(b.did), a)
    # Splice in B's pubkey to fake the fingerprint match; signature is still A's.
    forged = signed_by_a.model_copy(update={"public_key": b.public_key})
    assert verify_call(forged) is False


# --- IdentityLayer ----------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_layer_allows_valid_self_signed_on_personal() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    layer = IdentityLayer(registry={}, require_registered=False)
    signed = sign_call(_unsigned_call(ident.did), ident)
    decision = await layer.evaluate(signed, _ctx("personal"))
    assert decision.outcome == "allow"


@pytest.mark.asyncio
async def test_identity_layer_denies_unsigned() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    layer = IdentityLayer(registry={}, require_registered=False)
    decision = await layer.evaluate(_unsigned_call(ident.did), _ctx("personal"))
    assert decision.is_deny()
    assert decision.layer == "identity"


@pytest.mark.asyncio
async def test_identity_layer_enterprise_denies_unregistered_agent() -> None:
    """Self-signed is fine for auth, but enterprise admits only registered DIDs."""
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    layer = IdentityLayer(registry={}, require_registered=True)
    signed = sign_call(_unsigned_call(ident.did), ident)
    decision = await layer.evaluate(signed, _ctx("enterprise"))
    assert decision.is_deny()


@pytest.mark.asyncio
async def test_identity_layer_enterprise_allows_registered_agent() -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    layer = IdentityLayer(registry={ident.did: ident.public_key}, require_registered=True)
    signed = sign_call(_unsigned_call(ident.did), ident)
    decision = await layer.evaluate(signed, _ctx("enterprise"))
    assert decision.outcome == "allow"


@pytest.mark.asyncio
async def test_identity_layer_denies_registered_did_with_wrong_key() -> None:
    """DID is on the allowlist, but the registered pubkey ≠ the call's key."""
    victim = AgentIdentity.generate(org="default", agent_type="executor")
    attacker = AgentIdentity.generate(org="default", agent_type="executor")
    # Registry maps victim DID -> victim pubkey. Attacker signs a call claiming
    # the victim DID; fingerprint mismatch AND registry-key mismatch → deny.
    layer = IdentityLayer(registry={victim.did: victim.public_key}, require_registered=True)
    forged = sign_call(_unsigned_call(victim.did), attacker)
    decision = await layer.evaluate(forged, _ctx("enterprise"))
    assert decision.is_deny()


# --- build_pipeline wires identity first, at every tier ---------------------


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_build_pipeline_prepends_identity_layer(tier: str) -> None:
    pipe = build_pipeline(tier=tier)  # type: ignore[arg-type]
    assert pipe.layers[0].name == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
async def test_pipeline_denies_unsigned_call_every_tier(tier: str) -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    registry = {ident.did: ident.public_key}
    pipe = build_pipeline(tier=tier, agent_registry=registry)  # type: ignore[arg-type]
    decision = await pipe.evaluate(_unsigned_call(ident.did), _ctx(tier))
    assert decision.is_deny()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
async def test_pipeline_allows_signed_registered_call(tier: str) -> None:
    ident = AgentIdentity.generate(org="default", agent_type="executor")
    registry = {ident.did: ident.public_key}
    pipe = build_pipeline(tier=tier, agent_registry=registry)  # type: ignore[arg-type]
    signed = sign_call(_unsigned_call(ident.did), ident)
    decision = await pipe.evaluate(signed, _ctx(tier))
    assert decision.outcome == "allow"
