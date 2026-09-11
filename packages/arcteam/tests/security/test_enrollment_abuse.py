"""H-040 enrollment abuse battery — every foreign-admission attack fails closed.

The admission chokepoint ships in Slice 1, so per the project rule it is
incomplete without its abuse cases (threat-surface.md). Each case drives the REAL
production boundary — ``EntityRegistry.register`` (admission) or
``guard_dispatch`` (start) — and must fail closed:

  1. forged grant signature
  2. self-supplied approver key (grant verified against a key the member supplies
     rather than the trust-store operator key)
  3. replayed enrollment (nonce reuse / duplicate DID)
  4. tampered ``Entity.public_key`` after signing (TOCTOU)
  5. un-enrolled-member dispatch attempt

Registered in ``tests/run_adversarial_tests.py`` under the H-040 scenario.
"""

from __future__ import annotations

import pytest
from arctrust.identity import AgentIdentity
from arctrust.policy import (
    OperatorApprovalAuthority,
    enrollment_to_wire,
    sign_enrollment_grant,
)
from arctrust.signer import InProcessSigner

from arcteam.audit import AuditLogger
from arcteam.harness.enrollment import EnrollmentDenied, guard_dispatch
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType


def _operator() -> OperatorApprovalAuthority:
    return OperatorApprovalAuthority(AgentIdentity.generate(org="operator", agent_type="approver"))


def _resolver_for(operator: OperatorApprovalAuthority):
    """A trust store that knows ONE operator — the deployment operator, and no other."""

    def resolve(approver_did: str) -> bytes:
        if approver_did == operator.did:
            return operator.public_key
        raise KeyError(f"operator {approver_did!r} not in trust store")

    return resolve


def _enrolled(operator: OperatorApprovalAuthority, *, handle: str = "hermes", nonce: str = "n-1") -> tuple[Entity, AgentIdentity]:
    member = AgentIdentity.generate(org="acme", agent_type="hermes")
    grant = sign_enrollment_grant(
        operator=operator,
        did=member.did,
        handle=handle,
        harness="hermes",
        member_public_key=member.public_key,
        capabilities=frozenset({"chat"}),
        clearance="UNCLASSIFIED",
        audit_mode="boundary",
        not_before="2026-08-29T00:00:00+00:00",
        nonce=nonce,
    )
    entity = Entity(
        did=member.did,
        handle=handle,
        id=f"agent://{handle}",
        name=handle.title(),
        type=EntityType.AGENT,
        public_key=member.public_key.hex(),
        harness="hermes",
        enrollment=enrollment_to_wire(grant),
    )
    return entity, member


async def _registry(operator: OperatorApprovalAuthority) -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    return EntityRegistry(backend, audit, resolve_operator_key=_resolver_for(operator))


async def test_forged_grant_signature_is_refused_at_admission() -> None:
    operator = _operator()
    registry = await _registry(operator)
    entity, _ = _enrolled(operator)
    forged = dict(entity.enrollment or {})
    forged["signature"] = "Zm9yZ2Vk"  # base64 "forged"
    with pytest.raises(EnrollmentDenied):
        await registry.register(entity.model_copy(update={"enrollment": forged}))


async def test_self_supplied_approver_key_is_refused_at_admission() -> None:
    """A grant minted by a rogue authority NOT in the trust store is refused."""
    operator = _operator()
    registry = await _registry(operator)
    rogue = _operator()  # attacker's own authority — the resolver has no key for it
    entity, _ = _enrolled(rogue)
    with pytest.raises(EnrollmentDenied):
        await registry.register(entity)


async def test_replayed_enrollment_duplicate_did_is_refused() -> None:
    """Re-submitting an already-admitted member (same DID) is rejected (replay)."""
    operator = _operator()
    registry = await _registry(operator)
    entity, _ = _enrolled(operator)
    await registry.register(entity)  # first admission — legitimate
    with pytest.raises(ValueError, match="already registered"):
        await registry.register(entity)  # replayed — duplicate DID refused


async def test_tampered_public_key_after_signing_is_refused_toctou() -> None:
    operator = _operator()
    registry = await _registry(operator)
    entity, _ = _enrolled(operator)
    attacker = AgentIdentity.generate(org="acme", agent_type="hermes")
    tampered = entity.model_copy(update={"public_key": attacker.public_key.hex()})
    with pytest.raises(EnrollmentDenied):
        await registry.register(tampered)


def test_unenrolled_member_dispatch_is_refused() -> None:
    operator = _operator()
    entity, _ = _enrolled(operator)
    unenrolled = entity.model_copy(update={"enrollment": None})
    with pytest.raises(EnrollmentDenied):
        guard_dispatch(unenrolled, resolve_operator_key=_resolver_for(operator))
