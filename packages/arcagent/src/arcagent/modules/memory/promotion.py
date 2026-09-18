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

from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.filter import effective_score
from arcmemory.promotion.router import PromotionDecision, route
from arcmemory.types import Entity, Insight, Procedure

from arcagent.knowledge import (
    KnowledgeAccess,
    KnowledgeRef,
    PromotionSource,
    SharedKnowledgePort,
)

#: The scored private items a promotion pass may consider.
PromotableItem = Insight | Procedure | Entity


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
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
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
    access: KnowledgeAccess,
) -> KnowledgeRef:
    """Build the source for ``item`` and promote it through ``port``.

    The origin DID travels on ``access.caller_did`` (authoritative runtime
    context, never model-supplied), so the shared store can attribute the item.
    """
    source = to_promotion_source(item)
    return await port.promote(source, access)


async def run_promotion_pass(
    items: Iterable[PromotableItem],
    *,
    cfg: PromotionConfig,
    port: SharedKnowledgePort,
    access: KnowledgeAccess,
    other_person_check: Callable[[str], bool] | None = None,
) -> list[KnowledgeRef]:
    """Route each scored item and act on the verdict; return the promoted refs.

    Promotion is opt-in: with ``cfg.enabled`` False nothing leaves the private
    scope (REQ-447). Otherwise each item's effective score is
    ``effective_score(item.personal_score, <body>, other_person_check=...)`` over
    the SAME body text that becomes ``PromotionSource.content`` — so the raise-only
    privacy filter scores exactly what would ship. The router then decides:

    * ``AUTO``  -> promote immediately through ``port``;
    * ``NEVER`` -> skip (the item stays DID-scoped and private);
    * ``APPROVE`` -> enqueue for operator approval (T-1129); not promoted here.
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
            promoted.append(await port.promote(source, access))
        # APPROVE -> operator-approval queue lands in T-1129; NEVER -> stay private.
    return promoted


__all__ = [
    "PromotableItem",
    "promote_item",
    "run_promotion_pass",
    "to_promotion_source",
]
