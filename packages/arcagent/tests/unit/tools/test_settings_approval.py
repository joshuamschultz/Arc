"""SecurityChangeApproval (SPEC-085 COMP-007, REQ-465).

A security-relevant settings change (policy, identity, credentials) must not
apply silently. It creates a PENDING row on the existing mechanical
operator-approval subsystem (arcstore.approvals) and only becomes usable once
the operator grants it -- callable at the arcagent layer without importing
arcui/arccli.

Contract under test (arcagent.tools.settings_approval, module absent -> RED):
  - is_security_relevant(field) classifies a dotted config id.
  - request_settings_approval(...) creates a PENDING row binding the exact
    field+value change (arguments + call_hash).
  - verify_settings_grant(pending, operator_did=...) is fail-closed: only a
    still-``approved`` row carrying a grant that ``arctrust.policy.
    verify_approval`` accepts AND whose ``approver_did`` matches the given
    operator returns True.
"""

from __future__ import annotations

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from arctrust.policy import OperatorApprovalAuthority, grant_to_wire, sign_approval_for_hash
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.tools.settings_approval import (
    is_security_relevant,
    request_settings_approval,
    verify_settings_grant,
)

pytestmark = pytest.mark.asyncio

_AGENT_DID = "did:arc:example:org:agent:abc"


def _operator() -> OperatorApprovalAuthority:
    return OperatorApprovalAuthority(InProcessSigner(bytes(SigningKey.generate())))


async def _store() -> tuple[ApprovalStore, FakeBackend]:
    be = FakeBackend()
    await be.start()
    return ApprovalStore(be), be


class TestIsSecurityRelevant:
    @pytest.mark.parametrize(
        "field",
        [
            "security.audit_mode",
            "identity.did_prefix",
            "vault.token",
            "tools.policy.timeout_seconds",
        ],
    )
    def test_security_relevant_fields_are_true(self, field: str) -> None:
        assert is_security_relevant(field) is True

    @pytest.mark.parametrize("field", ["ui.theme", "agent.type", "llm.model"])
    def test_benign_fields_are_false(self, field: str) -> None:
        assert is_security_relevant(field) is False


class TestRequestSettingsApproval:
    async def test_creates_pending_row_bound_to_the_exact_change(self) -> None:
        store, be = await _store()
        try:
            pending = await request_settings_approval(
                field="security.audit_mode",
                value="strict",
                agent_did=_AGENT_DID,
                store=store,
            )
            assert pending.status == "pending"
            assert pending.agent_did == _AGENT_DID
            assert pending.arguments.get("field") == "security.audit_mode"
            assert pending.arguments.get("value") == "strict"
            assert pending.call_hash

            got = await store.get(pending.id)
            assert got is not None
            assert got.status == "pending"
            assert got.call_hash == pending.call_hash
        finally:
            await be.stop()

    async def test_different_values_bind_to_different_call_hashes(self) -> None:
        store, be = await _store()
        try:
            a = await request_settings_approval(
                field="security.audit_mode", value="strict", agent_did=_AGENT_DID, store=store
            )
            b = await request_settings_approval(
                field="security.audit_mode", value="lenient", agent_did=_AGENT_DID, store=store
            )
            assert a.call_hash != b.call_hash
        finally:
            await be.stop()


class TestVerifySettingsGrant:
    async def test_true_for_a_valid_operator_approved_grant(self) -> None:
        store, be = await _store()
        try:
            operator = _operator()
            pending = await request_settings_approval(
                field="identity.did_prefix",
                value="did:arc:new",
                agent_did=_AGENT_DID,
                store=store,
            )
            grant = sign_approval_for_hash(pending.call_hash, operator)
            await store.resolve(
                pending.id,
                status="approved",
                actor_did=operator.did,
                resolved_by=operator.did,
                grant=grant_to_wire(grant),
            )
            resolved = await store.get(pending.id)
            assert resolved is not None

            assert verify_settings_grant(resolved, operator_did=operator.did) is True
        finally:
            await be.stop()

    async def test_false_while_the_row_is_still_pending(self) -> None:
        store, be = await _store()
        try:
            operator = _operator()
            pending = await request_settings_approval(
                field="vault.token", value="new-token", agent_did=_AGENT_DID, store=store
            )

            assert verify_settings_grant(pending, operator_did=operator.did) is False
        finally:
            await be.stop()

    async def test_false_when_the_row_was_denied(self) -> None:
        store, be = await _store()
        try:
            operator = _operator()
            pending = await request_settings_approval(
                field="tools.policy.timeout_seconds",
                value="600",
                agent_did=_AGENT_DID,
                store=store,
            )
            await store.resolve(
                pending.id, status="denied", actor_did=operator.did, resolved_by=operator.did
            )
            resolved = await store.get(pending.id)
            assert resolved is not None

            assert verify_settings_grant(resolved, operator_did=operator.did) is False
        finally:
            await be.stop()

    async def test_false_when_the_grant_was_signed_by_a_different_operator(self) -> None:
        # Technically valid signature, wrong approver -- the gate must pin to
        # the specific deployment operator, not merely "not the agent" (ASI09).
        store, be = await _store()
        try:
            operator = _operator()
            impostor = _operator()
            pending = await request_settings_approval(
                field="security.audit_mode", value="strict", agent_did=_AGENT_DID, store=store
            )
            grant = sign_approval_for_hash(pending.call_hash, impostor)
            await store.resolve(
                pending.id,
                status="approved",
                actor_did=impostor.did,
                resolved_by=impostor.did,
                grant=grant_to_wire(grant),
            )
            resolved = await store.get(pending.id)
            assert resolved is not None

            assert verify_settings_grant(resolved, operator_did=operator.did) is False
        finally:
            await be.stop()

    async def test_false_when_the_grant_is_bound_to_a_different_change(self) -> None:
        # Tamper: a grant signed over a DIFFERENT field/value's call_hash must
        # not unlock this row, even with a valid operator and "approved" status.
        store, be = await _store()
        try:
            operator = _operator()
            original = await request_settings_approval(
                field="security.audit_mode", value="strict", agent_did=_AGENT_DID, store=store
            )
            other = await request_settings_approval(
                field="security.audit_mode", value="lenient", agent_did=_AGENT_DID, store=store
            )
            tampered_grant = sign_approval_for_hash(other.call_hash, operator)
            await store.resolve(
                original.id,
                status="approved",
                actor_did=operator.did,
                resolved_by=operator.did,
                grant=grant_to_wire(tampered_grant),
            )
            resolved = await store.get(original.id)
            assert resolved is not None

            assert verify_settings_grant(resolved, operator_did=operator.did) is False
        finally:
            await be.stop()

    async def test_false_when_approved_status_carries_no_grant(self) -> None:
        # Defensive fail-closed: an "approved" row with no grant attached must
        # never verify, regardless of how that inconsistent state arose.
        store, be = await _store()
        try:
            operator = _operator()
            pending = await request_settings_approval(
                field="security.audit_mode", value="strict", agent_did=_AGENT_DID, store=store
            )
            no_grant = pending.model_copy(update={"status": "approved", "grant": None})

            assert verify_settings_grant(no_grant, operator_did=operator.did) is False
        finally:
            await be.stop()
