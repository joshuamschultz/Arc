"""Signed operator approval, private source references, and release claims."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from arcmemory.promotion.config import PromotionConfig
from arcmemory.types import Insight
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from arctrust import AgentIdentity
from arctrust.policy import sign_approval_for_hash

from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource
from arcagent.modules.memory.promotion import to_promotion_source

_ACCESS = KnowledgeAccess(caller_did="did:arc:agent-a", clearance="unclassified")
_OPERATOR = AgentIdentity.generate("test", "human")


def _queue(store: ApprovalStore) -> Any:
    from arcmemory.promotion.approval import PromotionApprovalQueue

    return PromotionApprovalQueue(
        store,
        agent_did=_ACCESS.caller_did,
        operator_did=_OPERATOR.did,
        operator_public_key=_OPERATOR.public_key,
    )


async def _grant(queue: Any, item_id: str, decision: str = "approve") -> Any:
    row = await queue._store.get(queue._id(item_id))
    assert row is not None
    call_hash = (
        row.call_hash
        if decision == "approve"
        else queue._decision_hash(
            item_id=item_id,
            digest=row.arguments["digest"],
            classification=row.arguments["classification"],
            document_type=row.arguments["document_type"],
            decision="deny",
        )
    )
    return sign_approval_for_hash(call_hash, _OPERATOR)


# A clean, mid-scored insight -> effective_score 6 -> APPROVE band (5-7). It is
# neither auto-promotable (<=4) nor never (>=8): exactly the ambiguous middle a
# human must decide.
_APPROVE_ITEM = Insight(
    id="ambiguous-mid",
    statement="The team's internal deploy cadence is roughly once a week.",
    trigger="a deploy-cadence question",
    classification="unclassified",
    personal_score=6,
)


class _FakeSharedPort:
    """Records ``promote`` calls; a structural ``SharedKnowledgePort`` for the test."""

    def __init__(self) -> None:
        self.promoted: list[tuple[PromotionSource, KnowledgeAccess]] = []

    async def save(self, draft: Any, access: KnowledgeAccess) -> KnowledgeRef:  # pragma: no cover
        raise NotImplementedError

    async def read(self, reference: str, access: KnowledgeAccess) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def search(self, query: str, access: KnowledgeAccess) -> list[Any]:  # pragma: no cover
        return []

    async def promote(self, source: PromotionSource, access: KnowledgeAccess) -> KnowledgeRef:
        self.promoted.append((source, access))
        return KnowledgeRef(
            scope="shared",
            identifier=f"shared/{source.reference.identifier}",
            digest=source.digest,
        )

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None:  # pragma: no cover
        return None


class _FakePersonalPort:
    def __init__(self, source: PromotionSource) -> None:
        self.source = source

    async def export_for_promotion(
        self, reference: str, access: KnowledgeAccess
    ) -> PromotionSource:
        if (
            access.caller_did != _ACCESS.caller_did
            or reference != self.source.reference.identifier
        ):
            raise PermissionError("personal source owner mismatch")
        return self.source


async def test_approve_band_item_enqueues_then_bulk_approve_releases_it() -> None:
    """APPROVE queues (no promote); operator approve releases into the promote path."""

    from arcagent.modules.memory.promotion import (
        release_approved_promotions,
        run_promotion_pass,
        to_promotion_source,
    )

    backend = FakeBackend()
    await backend.start()
    try:
        queue = _queue(ApprovalStore(backend))
        port = _FakeSharedPort()

        refs = await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )

        # Nothing auto-promoted; the APPROVE-band item is parked for the operator.
        assert refs == []
        assert port.promoted == []
        pending = await queue.list_pending()
        assert len(pending) == 1
        assert pending[0].item_id == "ambiguous-mid"
        row = await queue._store.get(queue._id("ambiguous-mid"))
        assert row is not None
        assert _APPROVE_ITEM.statement not in str(row.model_dump())

        # Operator bulk-approves; the release step promotes it through the real port.
        await queue.approve("ambiguous-mid", grant=await _grant(queue, "ambiguous-mid"))
        released = await release_approved_promotions(
            approval_queue=queue,
            port=port,
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            access=_ACCESS,
        )

        assert len(released) == 1
        assert len(port.promoted) == 1
        promoted_source, _access = port.promoted[0]
        assert "ambiguous-mid" in promoted_source.reference.identifier
    finally:
        await backend.stop()


async def test_denied_item_is_remembered_and_not_requeued_on_reconsolidation() -> None:
    """A denied candidate is never promoted, and a later pass does NOT re-enqueue it."""

    from arcagent.modules.memory.promotion import run_promotion_pass

    backend = FakeBackend()
    await backend.start()
    try:
        queue = _queue(ApprovalStore(backend))
        port = _FakeSharedPort()

        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )
        pending = await queue.list_pending()
        assert len(pending) == 1

        # Operator denies -> the item is marked never and remembered.
        await queue.deny("ambiguous-mid", grant=await _grant(queue, "ambiguous-mid", "deny"))

        # The next consolidation re-scores the same item and runs the pass again.
        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )

        # Deny is sticky: no new pending row, and it never reaches the shared port.
        assert await queue.list_pending() == []
        assert port.promoted == []
    finally:
        await backend.stop()


async def test_changed_or_foreign_personal_source_cannot_release_approval() -> None:

    from arcagent.modules.memory.promotion import (
        release_approved_promotions,
        run_promotion_pass,
        to_promotion_source,
    )

    backend = FakeBackend()
    await backend.start()
    try:
        queue = _queue(ApprovalStore(backend))
        port = _FakeSharedPort()
        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )
        await queue.approve(_APPROVE_ITEM.id, grant=await _grant(queue, _APPROVE_ITEM.id))
        original = to_promotion_source(_APPROVE_ITEM)
        changed = replace(original, content="changed after approval")
        with pytest.raises(ValueError, match="source changed"):
            await release_approved_promotions(
                approval_queue=queue,
                port=port,
                personal=_FakePersonalPort(changed),
                access=_ACCESS,
            )
        with pytest.raises(PermissionError, match="owner mismatch"):
            await release_approved_promotions(
                approval_queue=queue,
                port=port,
                personal=_FakePersonalPort(original),
                access=KnowledgeAccess("did:arc:other", "unclassified"),
            )
        assert port.promoted == []
        assert len(await queue.list_approved()) == 1
    finally:
        await backend.stop()


async def test_concurrent_release_claims_one_shared_effect() -> None:

    from arcagent.modules.memory.promotion import (
        release_approved_promotions,
        run_promotion_pass,
        to_promotion_source,
    )

    backend = FakeBackend()
    await backend.start()
    try:
        queue = _queue(ApprovalStore(backend))
        port = _FakeSharedPort()
        personal = _FakePersonalPort(to_promotion_source(_APPROVE_ITEM))
        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )
        await queue.approve(_APPROVE_ITEM.id, grant=await _grant(queue, _APPROVE_ITEM.id))

        results = await asyncio.gather(
            *(
                release_approved_promotions(
                    approval_queue=queue,
                    port=port,
                    personal=personal,
                    access=_ACCESS,
                )
                for _ in range(2)
            )
        )

        assert sum(map(len, results)) == 1
        assert len(port.promoted) == 1
        row = await queue._store.get(queue._id(_APPROVE_ITEM.id))
        assert row is not None and row.status == "released"
    finally:
        await backend.stop()


async def test_crash_after_shared_effect_never_replays_approval() -> None:

    from arcagent.modules.memory.promotion import (
        release_approved_promotions,
        run_promotion_pass,
        to_promotion_source,
    )

    class _CrashAfterEffect(BaseException):
        pass

    class _CrashingPort(_FakeSharedPort):
        async def promote(self, source: PromotionSource, access: KnowledgeAccess) -> KnowledgeRef:
            await super().promote(source, access)
            raise _CrashAfterEffect

    backend = FakeBackend()
    await backend.start()
    try:
        queue = _queue(ApprovalStore(backend))
        port = _CrashingPort()
        personal = _FakePersonalPort(to_promotion_source(_APPROVE_ITEM))
        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            personal=_FakePersonalPort(to_promotion_source(_APPROVE_ITEM)),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )
        await queue.approve(_APPROVE_ITEM.id, grant=await _grant(queue, _APPROVE_ITEM.id))
        with pytest.raises(_CrashAfterEffect):
            await release_approved_promotions(
                approval_queue=queue,
                port=port,
                personal=personal,
                access=_ACCESS,
            )

        assert (
            await release_approved_promotions(
                approval_queue=queue,
                port=port,
                personal=personal,
                access=_ACCESS,
            )
            == []
        )
        assert len(port.promoted) == 1
        row = await queue._store.get(queue._id(_APPROVE_ITEM.id))
        assert row is not None and row.status == "releasing"
        assert await queue.claim_release(_APPROVE_ITEM.id) is None
    finally:
        await backend.stop()


async def test_forged_operator_and_cross_agent_replay_are_refused() -> None:
    from arcmemory.promotion.approval import PromotionApprovalQueue

    from arcagent.modules.memory.promotion import to_promotion_source

    backend = FakeBackend()
    await backend.start()
    try:
        queue = _queue(ApprovalStore(backend))
        source = to_promotion_source(_APPROVE_ITEM)
        await queue.enqueue(
            item_id=source.reference.identifier,
            title=source.title,
            content=source.content,
            classification=source.classification,
            document_type=source.document_type,
            digest=source.digest,
            effective_score=6,
            access=_ACCESS,
        )
        grant = await _grant(queue, source.reference.identifier)
        forged = grant.model_copy(update={"approver_did": _OPERATOR.did, "signature": b"forged"})
        with pytest.raises(PermissionError, match="operator promotion authority denied"):
            await queue.approve(source.reference.identifier, grant=forged)

        other_access = KnowledgeAccess("did:arc:other", "unclassified")
        other = PromotionApprovalQueue(
            ApprovalStore(backend),
            agent_did=other_access.caller_did,
            operator_did=_OPERATOR.did,
            operator_public_key=_OPERATOR.public_key,
        )
        await other.enqueue(
            item_id=source.reference.identifier,
            title=source.title,
            content=source.content,
            classification=source.classification,
            document_type=source.document_type,
            digest=source.digest,
            effective_score=6,
            access=other_access,
        )
        with pytest.raises(PermissionError, match="operator promotion authority denied"):
            await other.approve(source.reference.identifier, grant=grant)
        assert len(await other.list_pending()) == 1
    finally:
        await backend.stop()
