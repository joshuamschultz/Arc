"""H-040 Slice 1 — the reference foreign adapter, proven end to end.

One test walks the whole seam for the read-only ``hermes`` reference member:

    enroll (signed, chokepoint 1) -> badge (roster) -> @mention/deliver ->
    dispatch OUT-OF-PROCESS (Posture A) -> reply -> audit (arc-side WORM chain)

If any link breaks — enrollment does not verify, the badge is missing, the
subprocess does not run, the reply is not delivered, or the audit chain does not
record it — this fails. It is the Planner's merge-gate proof that a foreign type
is a first-class fleet member.
"""

from __future__ import annotations

import pytest
from arcteam.audit import AUDIT_COLLECTION, AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.harness.agent_type import find_agent_type
from arcteam.harness.enrollment import guard_dispatch
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType, Message
from arctrust.identity import AgentIdentity
from arctrust.policy import (
    OperatorApprovalAuthority,
    enrollment_to_wire,
    sign_enrollment_grant,
)
from arctrust.signer import InProcessSigner

from arcgateway import team_roster

pytestmark = pytest.mark.asyncio


def _resolver_for(operator: OperatorApprovalAuthority):
    def resolve(approver_did: str) -> bytes:
        if approver_did == operator.did:
            return operator.public_key
        raise KeyError(approver_did)

    return resolve


async def test_hermes_foreign_member_end_to_end() -> None:
    operator = OperatorApprovalAuthority(
        AgentIdentity.generate(org="operator", agent_type="approver")
    )
    resolver = _resolver_for(operator)

    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit, resolve_operator_key=resolver)

    # -- 1. ENROLL (operator-signed, chokepoint 1 verifies) ------------------
    hermes_id = AgentIdentity.generate(org="acme", agent_type="hermes")
    grant = sign_enrollment_grant(
        operator=operator,
        did=hermes_id.did,
        handle="hermes",
        harness="hermes",
        member_public_key=hermes_id.public_key,
        capabilities=frozenset({"chat"}),
        clearance="UNCLASSIFIED",
        audit_mode="boundary",
        not_before="2026-08-29T00:00:00+00:00",
        nonce="n-hermes-e2e",
    )
    hermes_entity = Entity(
        did=hermes_id.did,
        handle="hermes",
        id="agent://hermes",
        name="Hermes",
        type=EntityType.AGENT,
        public_key=hermes_id.public_key.hex(),
        harness="hermes",
        enrollment=enrollment_to_wire(grant),
    )
    await registry.register(hermes_entity)  # chokepoint 1 — verifies or raises

    # A native human/agent recipient of the reply.
    josh_id = AgentIdentity.generate(org="acme", agent_type="executor")
    await registry.register(
        Entity(
            did=josh_id.did,
            handle="josh",
            id="agent://josh",
            name="Josh",
            type=EntityType.AGENT,
            public_key=josh_id.public_key.hex(),
        )
    )

    # -- 2. BADGE (foreign member surfaces in the roster with its harness) ----
    roster = team_roster.merge_foreign_members([], [hermes_entity], online_ids=set())
    hermes_row = next(r for r in roster if r.did == hermes_id.did)
    assert hermes_row.harness == "hermes"
    assert hermes_row.agent_id == "hermes"

    # -- 3. BUILD the live member (chokepoint 3 verifies before construction) -
    guard_dispatch(hermes_entity, resolve_operator_key=resolver)
    agent_type = find_agent_type("hermes")
    assert agent_type is not None
    hermes_messenger = MessagingService(
        backend, registry, audit, signer=MessageSigner.from_identity(hermes_id)
    )
    member = agent_type.build(entity=hermes_entity, messenger=hermes_messenger)
    assert member.harness == "hermes"

    # -- 4/5. @MENTION -> DELIVER -> DISPATCH OUT-OF-PROCESS -> REPLY ---------
    mention = Message(
        sender="agent://josh",
        to=["agent://hermes"],
        body="@hermes are you there?",
        mentions=["hermes"],
    )
    await member.deliver(mention)  # spawns the subprocess worker, posts the reply

    # -- 6. REPLY delivered back to josh -------------------------------------
    received = await hermes_messenger.receive("arc.agent.josh", "agent://josh")
    assert received, "hermes did not deliver a reply to josh"
    reply = received[-1]
    assert "[hermes:hermes] read-only ack:" in reply.body
    assert reply.signer_did == hermes_id.did  # signed by the member's own key (Option A)

    # -- 7. AUDIT (arc-side WORM chain records enrollment + the reply send) ---
    valid, _last = await audit.verify_chain()
    assert valid, "audit chain failed to verify"
    records = await backend.read_stream(AUDIT_COLLECTION, "audit", after_seq=0, limit=1000)
    events = [r.get("event_type", "") for r in records]
    enroll_records = [
        r for r in records if r.get("event_type") == "entity.registered" and r.get("target_id") == hermes_id.did
    ]
    assert enroll_records, "no enrollment audit record for hermes"
    assert "harness=hermes" in enroll_records[0].get("detail", "")
    assert "message.sent" in events, events
