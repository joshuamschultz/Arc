"""Foreign-harness enrollment: pending row on register, signed grant on approve.

H-040 §3.3 reuses the mechanical-approval spine (SPEC-035): ``arc team register
--harness <foreign>`` writes a PENDING enrollment row instead of registering the
member directly; ``arc approve <id>`` mints the operator-signed
:class:`~arctrust.policy.EnrollmentGrant` and writes the verified ``Entity`` to
the registry. Native ``arcagent`` registration is unchanged — only a foreign
harness is held for operator signature.

This module holds the pure helpers (build the row, sign the grant, build the
entity) so the two CLI surfaces share one implementation and can be unit-tested
without a live bus.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from arcstore.approvals import PendingApproval
from arcteam.types import Entity, EntityType
from arctrust.canonical import canonical_json
from arctrust.identity import did_from_public_key
from arctrust.policy import (
    ApprovalAuthority,
    EnrollmentGrant,
    enrollment_to_wire,
    sign_enrollment_grant,
)

ENROLL_TOOL = "enroll"


def enrollment_did(public_key: bytes, *, org: str, harness: str) -> str:
    """Derive the member DID from its own verify key (``did:arc:<org>:<harness>/<hash>``)."""
    return did_from_public_key(public_key, org=org, agent_type=harness)


def build_pending_enrollment(
    *,
    handle: str,
    name: str,
    harness: str,
    public_key: bytes,
    capabilities: frozenset[str],
    clearance: str,
    audit_mode: str,
    org: str,
) -> PendingApproval:
    """Build the PENDING enrollment row an operator resolves with ``arc approve``.

    All enrollment facts are stashed in ``arguments`` (strings) so ``arc approve``
    can reconstruct and sign them verbatim; ``call_hash`` binds the eventual grant
    to exactly these facts.
    """
    did = enrollment_did(public_key, org=org, harness=harness)
    nonce = uuid.uuid4().hex
    not_before = datetime.now(UTC).isoformat()
    facts = {
        "did": did,
        "handle": handle,
        "name": name,
        "harness": harness,
        "member_public_key": public_key.hex(),
        "capabilities": ",".join(sorted(capabilities)),
        "clearance": clearance,
        "audit_mode": audit_mode,
        "not_before": not_before,
        "nonce": nonce,
        "org": org,
    }
    call_hash = canonical_json(facts).hex()
    return PendingApproval(
        id=f"enroll-{uuid.uuid4().hex[:12]}",
        agent_did=did,
        agent_label=handle,
        tool=ENROLL_TOOL,
        legs=[ENROLL_TOOL, harness],
        call_hash=call_hash,
        arguments=facts,
    )


def is_enrollment(row: PendingApproval) -> bool:
    """Whether a pending row is a foreign-harness enrollment request."""
    return row.tool == ENROLL_TOOL


def sign_enrollment_from_row(
    row: PendingApproval, operator: ApprovalAuthority
) -> tuple[EnrollmentGrant, Entity]:
    """Mint the operator grant for a pending enrollment row and build its Entity.

    The grant is signed over exactly the facts the row recorded; the returned
    Entity carries the grant in wire form on ``enrollment`` so registry admission
    (chokepoint 1) re-verifies it against the trust-store operator key.
    """
    facts = row.arguments
    public_key = bytes.fromhex(facts["member_public_key"])
    capabilities = frozenset(c for c in facts.get("capabilities", "").split(",") if c)
    grant = sign_enrollment_grant(
        operator=operator,
        did=facts["did"],
        handle=facts["handle"],
        harness=facts["harness"],
        member_public_key=public_key,
        capabilities=capabilities,
        clearance=facts["clearance"],
        audit_mode=facts["audit_mode"],
        not_before=facts["not_before"],
        nonce=facts["nonce"],
    )
    entity = Entity(
        did=facts["did"],
        handle=facts["handle"],
        id=f"agent://{facts['handle']}",
        name=facts.get("name") or facts["handle"],
        type=EntityType.AGENT,
        public_key=facts["member_public_key"],
        capabilities=sorted(capabilities),
        clearance=facts["clearance"],
        harness=facts["harness"],
        enrollment=enrollment_to_wire(grant),
    )
    return grant, entity


__all__ = [
    "ENROLL_TOOL",
    "build_pending_enrollment",
    "enrollment_did",
    "is_enrollment",
    "sign_enrollment_from_row",
]
