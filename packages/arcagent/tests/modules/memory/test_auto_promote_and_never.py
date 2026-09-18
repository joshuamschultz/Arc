"""T-1126 (SPEC-083 COMP-004/005/008) — auto-promote vs never routing.

RED intent: the promotion step routes each scored item through
``router.route(effective_score(...))`` and acts on the verdict:

- an AUTO item (low score, clean text) is handed to the shared port's ``promote``;
- a NEVER item (high score OR privacy-filter-flagged) is NOT promoted and stays
  only in the private store.

Assert with a fake ``SharedKnowledgePort``: ``promote`` is called exactly once —
for the low/clean item — and never for the high-scored or the secret-bearing item.

It fails today because the promotion pass does not exist
(``ModuleNotFoundError`` importing ``arcagent.modules.memory.promotion``).

Where the wiring lives (stated for the GREEN implementer, T-1127): the pass is
ARCAGENT-side, because it uses the ``arcagent.knowledge`` port that ``arcmemory``
may not import. It runs *after* a consolidation over the newly-scored items. Its
observable contract (tested here directly, so the RED does not need the whole
Consolidator harness — the full real-path consolidation → search on a second
agent is the E2E gate T-1134, which is what guards against a producers-unwired
regression):

    async def run_promotion_pass(
        items, *, cfg: PromotionConfig, port, access, other_person_check=None
    ) -> list[KnowledgeRef]

routing each item by ``route(effective_score(personal_score, body_text,
other_person_check=...), cfg)``: AUTO -> ``port.promote``; NEVER -> skip; and it
promotes nothing when ``cfg.enabled`` is False (REQ-447). The ``body_text`` fed to
the filter is the same text that becomes ``PromotionSource.content``.
"""

from __future__ import annotations

from typing import Any

from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource
from arcmemory.promotion.config import PromotionConfig
from arcmemory.types import Insight


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
        return KnowledgeRef(scope="shared", identifier="shared/1", digest=source.digest)

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None:  # pragma: no cover
        return None


_ACCESS = KnowledgeAccess(caller_did="did:arc:agent-a", clearance="unclassified")

# A low, clean insight -> effective_score stays low -> AUTO.
_AUTO_ITEM = Insight(
    id="clean-low",
    statement="The month-end close runs on the third business day.",
    trigger="a close-timing question",
    classification="unclassified",
    personal_score=2,
)
# A high-scored insight -> NEVER by score.
_NEVER_BY_SCORE = Insight(
    id="private-high",
    statement="An intensely personal reflection best kept private.",
    trigger="a private situation",
    classification="unclassified",
    personal_score=9,
)
# A low score but the body carries a secret -> the raise-only filter floors it to
# the never band, so it is NEVER despite the low personal_score.
_NEVER_BY_FILTER = Insight(
    id="secret-low",
    statement="The prod database password is stored in the ops vault.",
    trigger="a credentials question",
    classification="unclassified",
    personal_score=2,
)


async def test_auto_item_is_promoted_never_items_are_not() -> None:
    """Only the low/clean item reaches ``promote``; scored-high and secret do not."""
    from arcagent.modules.memory.promotion import run_promotion_pass

    port = _FakeSharedPort()

    await run_promotion_pass(
        [_AUTO_ITEM, _NEVER_BY_SCORE, _NEVER_BY_FILTER],
        cfg=PromotionConfig(enabled=True),
        port=port,
        access=_ACCESS,
    )

    assert len(port.promoted) == 1
    (source, _access) = port.promoted[0]
    assert "clean-low" in source.reference.identifier


async def test_disabled_promotion_never_touches_the_shared_port() -> None:
    """With promotion OFF (REQ-447), even a clean/low item stays private."""
    from arcagent.modules.memory.promotion import run_promotion_pass

    port = _FakeSharedPort()

    await run_promotion_pass(
        [_AUTO_ITEM],
        cfg=PromotionConfig(enabled=False),
        port=port,
        access=_ACCESS,
    )

    assert port.promoted == []
