"""EnrollmentGrant — operator-signed proof that admits a foreign fleet member.

H-040 Slice 1, chokepoint-1 crypto. Mirrors the ScenarioGrant tests: a grant is
minted under the OPERATOR authority over the canonical enrollment facts, and
:func:`verify_enrollment` is fail-closed against every tampering an attacker can
attempt at admission — a forged signature, a member-supplied approver key, a
swapped member public key (TOCTOU), or facts that do not match the grant.
"""

from __future__ import annotations

import pytest
from arctrust.identity import AgentIdentity
from arctrust.policy import (
    EnrollmentGrant,
    OperatorApprovalAuthority,
    sign_enrollment_grant,
    verify_enrollment,
)


def _operator() -> OperatorApprovalAuthority:
    return OperatorApprovalAuthority(AgentIdentity.generate(org="operator", agent_type="approver"))


def _member() -> AgentIdentity:
    return AgentIdentity.generate(org="acme", agent_type="hermes")


def _facts(member: AgentIdentity) -> dict[str, object]:
    return {
        "did": member.did,
        "handle": "hermes",
        "harness": "hermes",
        "member_public_key": member.public_key,
        "capabilities": frozenset({"chat"}),
        "clearance": "UNCLASSIFIED",
        "audit_mode": "boundary",
        "not_before": "2026-08-29T00:00:00+00:00",
        "nonce": "n-0001",
    }


def test_signed_grant_verifies_against_the_operator_key() -> None:
    operator, member = _operator(), _member()
    grant = sign_enrollment_grant(operator=operator, **_facts(member))
    assert verify_enrollment(
        grant,
        did=member.did,
        handle="hermes",
        harness="hermes",
        member_public_key=member.public_key,
        operator_public_key=operator.public_key,
    )


def test_forged_signature_is_rejected() -> None:
    operator, member = _operator(), _member()
    grant = sign_enrollment_grant(operator=operator, **_facts(member))
    forged = grant.model_copy(update={"signature": b"\x00" * 64})
    assert not verify_enrollment(
        forged,
        did=member.did,
        handle="hermes",
        harness="hermes",
        member_public_key=member.public_key,
        operator_public_key=operator.public_key,
    )


def test_self_supplied_approver_key_is_rejected() -> None:
    """A grant signed by a key the MEMBER supplies must not verify.

    The attacker mints a valid signature under a rogue authority of their own
    and stamps that key as ``approver_public_key``. verify_enrollment is handed
    the real operator key from the trust store; the grant's approver key differs,
    so it fails closed even though the signature is internally consistent.
    """
    member = _member()
    rogue_operator = _operator()  # attacker-controlled authority, not in the trust store
    grant = sign_enrollment_grant(operator=rogue_operator, **_facts(member))
    real_operator = _operator()  # the deployment operator (trust-store key)
    assert grant.approver_public_key != real_operator.public_key
    assert not verify_enrollment(
        grant,
        did=member.did,
        handle="hermes",
        harness="hermes",
        member_public_key=member.public_key,
        operator_public_key=real_operator.public_key,
    )


def test_tampered_member_public_key_is_rejected() -> None:
    """Swapping the entity's public key after signing breaks verification (TOCTOU)."""
    operator, member = _operator(), _member()
    grant = sign_enrollment_grant(operator=operator, **_facts(member))
    attacker = _member()
    assert not verify_enrollment(
        grant,
        did=member.did,
        handle="hermes",
        harness="hermes",
        member_public_key=attacker.public_key,  # swapped
        operator_public_key=operator.public_key,
    )


def test_mismatched_facts_are_rejected() -> None:
    operator, member = _operator(), _member()
    grant = sign_enrollment_grant(operator=operator, **_facts(member))
    assert not verify_enrollment(
        grant,
        did=member.did,
        handle="not-hermes",  # handle mismatch
        harness="hermes",
        member_public_key=member.public_key,
        operator_public_key=operator.public_key,
    )


def test_member_cannot_be_its_own_approver() -> None:
    """No self-enrollment — a grant the member signed with its OWN key fails.

    Worst case: the member's key is even handed in as the operator anchor. The
    member-key == approver-key equality is the self-blessing guard (ASI09), so
    admission still fails closed.
    """
    member = _member()
    member_authority = OperatorApprovalAuthority(member)  # member signs as its own approver
    grant = sign_enrollment_grant(operator=member_authority, **_facts(member))
    assert grant.approver_public_key == member.public_key
    assert not verify_enrollment(
        grant,
        did=member.did,
        handle="hermes",
        harness="hermes",
        member_public_key=member.public_key,
        operator_public_key=member.public_key,
    )


def test_grant_has_no_expiry_field() -> None:
    """§13 ruling: revocation is the sole invalidation — a grant carries no TTL."""
    assert "expires_at" not in EnrollmentGrant.model_fields
    assert "not_after" not in EnrollmentGrant.model_fields
    assert "ttl" not in EnrollmentGrant.model_fields
