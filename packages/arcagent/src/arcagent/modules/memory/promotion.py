"""Promotion bridge — map a scored private memory item to a shared source and
route it through the shared-knowledge port (SPEC-083 COMP-005 / COMP-004).

ARCAGENT-side by construction. ``PromotionSource`` and ``SharedKnowledgePort``
live in :mod:`arcagent.knowledge`, and ``arcmemory`` is a DAG leaf that may not
import ``arcagent`` (``arcmemory/tests/architecture/test_no_arcagent_import.py``).
This module is the one layer that legally knows both the arcmemory item types
(the optional ``arcagent[memory]`` extra) and the ``arcagent.knowledge`` ports,
so it owns the mapping and the routing. ``arcagent -> arcmemory`` is the allowed
direction, so importing ``arcmemory`` here is fine; the module is only reached
when promotion is actually wired (arcmemory present), never at NullBrain load.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable

from arcmemory.promotion.approval import PromotionApprovalQueue
from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.filter import effective_score
from arcmemory.promotion.router import PromotionDecision, route
from arcmemory.types import Entity, Insight, Procedure

from arcagent.knowledge import (
    KnowledgeAccess,
    KnowledgeRef,
    PersonalKnowledgePort,
    PromotionSource,
    SharedKnowledgePort,
)

#: The scored private items a promotion pass may consider.
PromotableItem = Insight | Procedure | Entity


class PromotionReleaseOutcomeUnknownError(RuntimeError):
    """A shared effect may have occurred; an operator must reconcile the claim."""


class MemoryPromotionUnavailableError(RuntimeError):
    """Trusted score, source, or fleet authority was not composed for promotion."""


def _describe(item: PromotableItem) -> tuple[str, str, str, str]:
    """Return ``(identifier, title, content, document_type)`` for a promotable item.

    The ``content`` is the glass-box body promoted into the shared store and the
    SAME text the privacy filter scores (see :func:`run_promotion_pass`). Each
    branch renders the item's own body faithfully; ``content`` is never empty, so
    the digest and title stay meaningful.
    """
    if isinstance(item, Insight):
        return item.id, item.statement, item.statement, "insight"
    if isinstance(item, Procedure):
        steps = "\n".join(f"- {step.text}" for step in item.steps)
        content = "\n\n".join(part for part in (item.when_to_use, steps) if part)
        return item.slug, item.title, content or item.title, "procedure"
    facts = "\n".join(f"- {fact.predicate}: {fact.value}" for fact in item.facts)
    return item.slug, item.name, facts or item.name, "entity"


def to_promotion_source(item: PromotableItem) -> PromotionSource:
    """Map a scored private item to a :class:`~arcagent.knowledge.PromotionSource`.

    Title and body come from the item, the item's ``classification`` is preserved
    verbatim (never laundered), and the source is attributable back to the origin
    item through a ``KnowledgeRef(scope="personal", identifier=<id/slug>)``. The
    digest is a content hash so the shared store can address the promoted bytes.
    """
    identifier, title, content, document_type = _describe(item)
    digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    return PromotionSource(
        reference=KnowledgeRef(scope="personal", identifier=identifier, digest=digest),
        digest=digest,
        content=content,
        classification=item.classification,
        title=title,
        document_type=document_type,
    )


async def promote_item(
    item: PromotableItem,
    *,
    port: SharedKnowledgePort,
    personal: PersonalKnowledgePort | None = None,
    access: KnowledgeAccess,
) -> KnowledgeRef:
    """Build the source for ``item`` and promote it through ``port``.

    The origin DID travels on ``access.caller_did`` (authoritative runtime
    context, never model-supplied), so the shared store can attribute the item.
    """
    source = await _verified_source(item, personal=personal, access=access)
    return await port.promote(source, access)


async def _verified_source(
    item: PromotableItem, *, personal: PersonalKnowledgePort | None,
    access: KnowledgeAccess,
) -> PromotionSource:
    if personal is None:
        raise MemoryPromotionUnavailableError("personal source authority is unavailable")
    expected = to_promotion_source(item)
    actual = await personal.export_for_promotion(expected.reference.identifier, access)
    if (
        actual.reference.scope != "personal"
        or actual.reference.identifier != expected.reference.identifier
        or actual.digest != expected.digest
        or actual.content.strip() != expected.content.strip()
        or actual.classification != expected.classification
        or actual.document_type != expected.document_type
        or actual.title != expected.title
    ):
        raise ValueError("personal source differs from scored bytes")
    return actual


async def run_promotion_pass(
    items: Iterable[PromotableItem],
    *,
    cfg: PromotionConfig,
    port: SharedKnowledgePort,
    personal: PersonalKnowledgePort | None = None,
    access: KnowledgeAccess,
    other_person_check: Callable[[str], bool] | None = None,
    approval_queue: PromotionApprovalQueue | None = None,
) -> list[KnowledgeRef]:
    """Route each scored item and act on the verdict; return the promoted refs.

    Promotion is opt-in: with ``cfg.enabled`` False nothing leaves the private
    scope (REQ-447). Otherwise each item's effective score is
    ``effective_score(item.personal_score, <body>, other_person_check=...)`` over
    the SAME body text that becomes ``PromotionSource.content`` — so the raise-only
    privacy filter scores exactly what would ship. The router then decides:

    * ``AUTO`` -> typed unavailable until a verified score grant is composed;
    * ``NEVER`` -> skip (the item stays DID-scoped and private);
    * ``APPROVE`` -> enqueue a verified personal reference for signed operator review.
    """
    if not cfg.enabled:
        return []
    promoted: list[KnowledgeRef] = []
    for item in items:
        source = to_promotion_source(item)
        score = effective_score(
            item.personal_score, source.content, other_person_check=other_person_check
        )
        decision = route(score, cfg)
        if decision is PromotionDecision.AUTO:
            raise MemoryPromotionUnavailableError(
                "automatic promotion requires a verified score grant"
            )
        elif decision is PromotionDecision.APPROVE and approval_queue is not None:
            verified = await _verified_source(item, personal=personal, access=access)
            await approval_queue.enqueue(
                item_id=verified.reference.identifier,
                title=verified.title,
                content=verified.content,
                classification=verified.classification,
                document_type=verified.document_type,
                digest=verified.digest,
                effective_score=score,
                access=access,
            )
    return promoted


async def release_approved_promotions(
    *, approval_queue: PromotionApprovalQueue, port: SharedKnowledgePort,
    personal: PersonalKnowledgePort, access: KnowledgeAccess,
) -> list[KnowledgeRef]:
    """Release only current, owner-authorized personal exports matching approval."""
    released: list[KnowledgeRef] = []
    for candidate in await approval_queue.list_approved():
        source = await personal.export_for_promotion(candidate.item_id, access)
        if (
            source.reference.scope != "personal"
            or source.reference.identifier != candidate.item_id
            or source.digest != candidate.digest
            or "sha256:" + hashlib.sha256(source.content.strip().encode()).hexdigest()
            != candidate.digest
            or source.classification != candidate.classification
            or source.document_type != candidate.document_type
        ):
            raise ValueError("approved personal source changed")
        token = await approval_queue.claim_release(candidate.item_id)
        if token is None:
            continue
        try:
            promoted = await port.promote(source, access)
        except Exception as error:
            await approval_queue.finish_release(
                candidate.item_id, token=token, outcome="outcome_unknown"
            )
            raise PromotionReleaseOutcomeUnknownError(
                "promotion release outcome requires reconciliation"
            ) from error
        await approval_queue.finish_release(
            candidate.item_id, token=token, outcome="released"
        )
        released.append(promoted)
    return released


__all__ = [
    "MemoryPromotionUnavailableError",
    "PromotableItem",
    "PromotionReleaseOutcomeUnknownError",
    "promote_item",
    "release_approved_promotions",
    "run_promotion_pass",
    "to_promotion_source",
]
