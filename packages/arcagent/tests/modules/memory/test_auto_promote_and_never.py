"""Unverified automatic scores are unavailable; private decisions stay private."""

from __future__ import annotations

from typing import Any

import pytest
from arcmemory.promotion.config import PromotionConfig
from arcmemory.types import Insight

from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource


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


class _FakePersonalPort:
    async def export_for_promotion(
        self, reference: str, access: KnowledgeAccess
    ) -> PromotionSource:
        from arcagent.modules.memory.promotion import to_promotion_source

        item = next(item for item in (_AUTO_ITEM, _NEVER_BY_SCORE, _NEVER_BY_FILTER) if item.id == reference)
        if access.caller_did != _ACCESS.caller_did:
            raise PermissionError("foreign agent")
        return to_promotion_source(item)


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


async def test_raw_auto_score_is_unavailable_and_never_items_stay_private() -> None:
    """An editable score cannot authorize sharing; high and secret items remain private."""
    from arcagent.modules.memory.promotion import (
        MemoryPromotionUnavailableError,
        run_promotion_pass,
    )

    port = _FakeSharedPort()

    with pytest.raises(MemoryPromotionUnavailableError):
        await run_promotion_pass(
            [_AUTO_ITEM], cfg=PromotionConfig(enabled=True), port=port,
            personal=_FakePersonalPort(), access=_ACCESS,
        )
    await run_promotion_pass(
        [_NEVER_BY_SCORE, _NEVER_BY_FILTER], cfg=PromotionConfig(enabled=True),
        port=port, personal=_FakePersonalPort(), access=_ACCESS,
    )
    assert port.promoted == []


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
