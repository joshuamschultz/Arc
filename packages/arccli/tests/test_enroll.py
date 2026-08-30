"""H-040 CLI enrollment helpers — register writes a row, approve signs a grant."""

from __future__ import annotations

from arctrust.identity import AgentIdentity, did_matches_pubkey
from arctrust.policy import OperatorApprovalAuthority, verify_enrollment

from arccli.commands.enroll import (
    ENROLL_TOOL,
    build_pending_enrollment,
    is_enrollment,
    sign_enrollment_from_row,
)


def _member() -> AgentIdentity:
    return AgentIdentity.generate(org="acme", agent_type="hermes")


def test_build_pending_enrollment_derives_did_from_the_supplied_key() -> None:
    member = _member()
    row = build_pending_enrollment(
        handle="hermes",
        name="Hermes",
        harness="hermes",
        public_key=member.public_key,
        capabilities=frozenset({"chat"}),
        clearance="UNCLASSIFIED",
        audit_mode="boundary",
        org="acme",
    )
    assert is_enrollment(row)
    assert row.tool == ENROLL_TOOL
    assert did_matches_pubkey(row.agent_did, member.public_key)
    assert row.arguments["member_public_key"] == member.public_key.hex()
    assert row.arguments["harness"] == "hermes"


def test_sign_enrollment_from_row_produces_a_verifiable_grant_and_entity() -> None:
    member = _member()
    operator = OperatorApprovalAuthority(
        AgentIdentity.generate(org="operator", agent_type="approver")
    )
    row = build_pending_enrollment(
        handle="hermes",
        name="Hermes",
        harness="hermes",
        public_key=member.public_key,
        capabilities=frozenset({"chat", "read"}),
        clearance="UNCLASSIFIED",
        audit_mode="boundary",
        org="acme",
    )
    grant, entity = sign_enrollment_from_row(row, operator)

    # The grant verifies against the operator's trust-store key.
    assert verify_enrollment(
        grant,
        did=entity.did,
        handle=entity.handle,
        harness=entity.harness,
        member_public_key=member.public_key,
        operator_public_key=operator.public_key,
    )
    # The entity carries the grant in wire form, ready for registry admission.
    assert entity.harness == "hermes"
    assert entity.enrollment is not None
    assert entity.public_key == member.public_key.hex()
    assert set(entity.capabilities) == {"chat", "read"}
