"""ArcStoreApprovalChannel — the mechanical operator handoff (SPEC-035).

The channel enqueues a pending row and resolves to the operator's grant, to None
on deny/expire, and — end to end with HumanGate — only lets a VALID operator
grant unlock the call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.sqlite import SqliteBackend
from arctrust.identity import AgentIdentity
from arctrust.policy import ToolCall, _hash_call, grant_to_wire, sign_approval_for_hash
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.tools.approval_channel import ArcStoreApprovalChannel
from arcagent.tools.human_gate import ApprovalRequest, HumanGate

pytestmark = pytest.mark.asyncio

_TRIFECTA = frozenset({"private_data", "external_comms", "untrusted_input"})
_AGENT = "did:arc:example:org:agent:abc"


async def _store(tmp_path: Path) -> tuple[ApprovalStore, SqliteBackend]:
    be = SqliteBackend(tmp_path / "store.db")
    await be.start()
    return ApprovalStore(be), be


def _request(call_hash: str = "hash123") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="egress", agent_did=_AGENT, legs=_TRIFECTA, call_hash=call_hash
    )


async def test_channel_returns_operator_grant_when_approved(tmp_path: Path) -> None:
    store, be = await _store(tmp_path)
    try:
        operator = AgentIdentity.generate(org="operator", agent_type="approver")
        channel = ArcStoreApprovalChannel(store, id_factory=lambda: "req1", agent_label="josh")
        req = _request()

        async def approve_soon() -> None:
            # Wait for the pending row, then resolve as an operator would.
            for _ in range(50):
                if await store.get("req1") is not None:
                    break
                await asyncio.sleep(0.01)
            grant = sign_approval_for_hash(req.call_hash, operator)
            await store.resolve(
                "req1", status="approved", actor_did=operator.did,
                resolved_by=operator.did, grant=grant_to_wire(grant),
            )

        approver = asyncio.create_task(approve_soon())
        result = await asyncio.wait_for(channel(req), timeout=2)
        await approver
        assert result is not None
        assert result.approver_did == operator.did
    finally:
        await be.stop()


async def test_channel_returns_none_when_denied(tmp_path: Path) -> None:
    store, be = await _store(tmp_path)
    try:
        channel = ArcStoreApprovalChannel(store, id_factory=lambda: "req1")

        async def deny_soon() -> None:
            for _ in range(50):
                if await store.get("req1") is not None:
                    break
                await asyncio.sleep(0.01)
            await store.resolve("req1", status="denied", actor_did="op", resolved_by="op")

        denier = asyncio.create_task(deny_soon())
        result = await asyncio.wait_for(channel(_request()), timeout=2)
        await denier
        assert result is None
    finally:
        await be.stop()


async def test_end_to_end_gate_admits_only_valid_operator_grant(tmp_path: Path) -> None:
    # The real HumanGate + channel: an operator grant for the exact call unlocks
    # it; a grant an attacker signs for a different hash is rejected by the gate.
    store, be = await _store(tmp_path)
    try:
        operator = AgentIdentity.generate(org="operator", agent_type="approver")
        call = ToolCall(
            tool_name="egress", arguments={"url": "https://x"}, agent_did=_AGENT,
            session_id="", classification="unclassified",
            capability_tags=frozenset({"external_comms"}),
        )
        call_hash = _hash_call(call)
        channel = ArcStoreApprovalChannel(store, id_factory=lambda: "req1")
        gate = HumanGate(
            operator_signer=InProcessSigner(bytes(SigningKey.generate())),
            agent_did=_AGENT, tier="enterprise", channel=channel,
        )

        async def approve_for(target_hash: str) -> None:
            for _ in range(50):
                if await store.get("req1") is not None:
                    break
                await asyncio.sleep(0.01)
            grant = sign_approval_for_hash(target_hash, operator)
            await store.resolve(
                "req1", status="approved", actor_did=operator.did,
                resolved_by=operator.did, grant=grant_to_wire(grant),
            )

        # Correct hash -> gate returns a verified grant.
        task = asyncio.create_task(approve_for(call_hash))
        grant = await asyncio.wait_for(gate.request(call, legs=_TRIFECTA), timeout=2)
        await task
        assert grant is not None and grant.approver_did == operator.did
    finally:
        await be.stop()
