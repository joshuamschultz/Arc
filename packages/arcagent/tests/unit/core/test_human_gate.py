"""SPEC-035 REQ-014/015/016 — human-approval gate unit tests.

The gate pauses a trifecta-completing call for explicit human approval, mints a
one-shot OPERATOR-signed token (never the agent DID — ASI09), fails closed on
timeout/denial, and never auto-approves at federal.
"""

from __future__ import annotations

import asyncio

import pytest
from arctrust.policy import PolicyContext, ToolCall, verify_approval
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.tools.human_gate import ApprovalRequest, HumanGate, HumanGateConfig

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
        async def approve(_req: ApprovalRequest) -> bool:
            return True

        async def deny(_req: ApprovalRequest) -> bool:
            return False

        signer = _operator_signer()
        agent_did = "did:arc:example:org:agent:abc"
        approving = HumanGate(operator_signer=signer, agent_did=agent_did, tier="enterprise",
                              channel=approve)
        denying = HumanGate(operator_signer=signer, agent_did=agent_did, tier="enterprise",
                            channel=deny)
        assert await approving.request(_call(), legs=_TRIFECTA) is not None
        assert await denying.request(_call(), legs=_TRIFECTA) is None

    async def test_channel_timeout_fails_closed(self) -> None:
        async def hang(_req: ApprovalRequest) -> bool:
            await asyncio.sleep(10)
            return True

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
        gate = HumanGate(operator_signer=signer, agent_did=agent_did, tier="personal",
                         config=HumanGateConfig(auto_approve=[_TRIFECTA]))
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
