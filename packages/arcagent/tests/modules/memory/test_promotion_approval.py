"""T-1128 (SPEC-083 COMP-006) — the APPROVE-band batched approval queue.

RED intent. The router already classifies a mid-score item as ``APPROVE`` (5-7),
but :func:`arcagent.modules.memory.promotion.run_promotion_pass` currently NO-OPS
that band (see its own docstring: "APPROVE -> operator-approval queue lands in
T-1129; not promoted here"). This file pins the behavior T-1129 must add:

1. An APPROVE-band item is ENQUEUED as an operator-approval candidate rather than
   promoted. The fake shared port's ``promote`` is NOT called for it.
2. A bulk **approve** RELEASES the queued item into the real promote path — the
   port's ``promote`` is then called exactly once for it.
3. A **deny** marks the item never, and is REMEMBERED: re-running the promotion
   pass on the same item does NOT re-enqueue it (no approval fatigue, REQ-440),
   and it is never promoted.

Intended API (stated for the GREEN implementer, T-1129 — leaf rule respected:
``arcmemory`` may NOT import ``arcagent``; the queue is arcmemory-side over the
SPEC-035 ``arcstore`` approval spine, storing only NEUTRAL fields; the release
that rebuilds an ``arcagent.knowledge.PromotionSource`` is arcagent-side):

- ``arcmemory.promotion.approval.PromotionApprovalQueue(store, *, agent_did)``
  wrapping an ``arcstore.approvals.ApprovalStore`` (a ``promotion`` kind), with:

    * ``async enqueue(*, item_id, title, content, classification,
        document_type, digest, effective_score, access) -> bool``
      — True if newly queued; False when the item is already pending OR was
      previously DENIED (deny is remembered);
    * ``async list_pending() -> list[<candidate>]`` — neutral candidate records
      each carrying ``item_id`` + the neutral fields + ``effective_score``;
    * ``async approve(item_id, *, actor_did) -> None``;
    * ``async deny(item_id, *, actor_did) -> None``.

- ``arcagent.modules.memory.promotion.run_promotion_pass(..., approval_queue=None)``
  — routes an APPROVE item to ``approval_queue.enqueue(...)`` instead of skipping.
- ``arcagent.modules.memory.promotion.release_approved_promotions(
      *, approval_queue, port, access) -> list[KnowledgeRef]``
  — for each APPROVED candidate, rebuild the ``PromotionSource`` from its neutral
  fields and call ``port.promote``; mark it released.

Fails today: ``arcmemory.promotion.approval`` does not exist, ``run_promotion_pass``
does not accept ``approval_queue``, and there is no ``release_approved_promotions``.
"""

from __future__ import annotations

from typing import Any

from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource
from arcmemory.promotion.config import PromotionConfig
from arcmemory.types import Insight
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

_ACCESS = KnowledgeAccess(caller_did="did:arc:agent-a", clearance="unclassified")
_OPERATOR = "did:arc:test:human/operator"

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
        return KnowledgeRef(scope="shared", identifier=f"shared/{source.reference.identifier}", digest=source.digest)

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None:  # pragma: no cover
        return None


async def test_approve_band_item_enqueues_then_bulk_approve_releases_it() -> None:
    """APPROVE queues (no promote); operator approve releases into the promote path."""
    from arcagent.modules.memory.promotion import (
        release_approved_promotions,
        run_promotion_pass,
    )
    from arcmemory.promotion.approval import PromotionApprovalQueue

    backend = FakeBackend()
    await backend.start()
    try:
        queue = PromotionApprovalQueue(ApprovalStore(backend), agent_did=_ACCESS.caller_did)
        port = _FakeSharedPort()

        refs = await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
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

        # Operator bulk-approves; the release step promotes it through the real port.
        await queue.approve("ambiguous-mid", actor_did=_OPERATOR)
        released = await release_approved_promotions(
            approval_queue=queue, port=port, access=_ACCESS
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
    from arcmemory.promotion.approval import PromotionApprovalQueue

    backend = FakeBackend()
    await backend.start()
    try:
        queue = PromotionApprovalQueue(ApprovalStore(backend), agent_did=_ACCESS.caller_did)
        port = _FakeSharedPort()

        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )
        pending = await queue.list_pending()
        assert len(pending) == 1

        # Operator denies -> the item is marked never and remembered.
        await queue.deny("ambiguous-mid", actor_did=_OPERATOR)

        # The next consolidation re-scores the same item and runs the pass again.
        await run_promotion_pass(
            [_APPROVE_ITEM],
            cfg=PromotionConfig(enabled=True),
            port=port,
            access=_ACCESS,
            approval_queue=queue,
        )

        # Deny is sticky: no new pending row, and it never reaches the shared port.
        assert await queue.list_pending() == []
        assert port.promoted == []
    finally:
        await backend.stop()
