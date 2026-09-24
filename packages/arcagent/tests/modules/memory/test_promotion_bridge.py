"""Promotion bridge requires a matching authorized personal source export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcmemory.types import Insight

from arcagent.brain import NullBrain
from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource
from arcagent.modules.memory._runtime import _State
from arcagent.modules.memory.config import MemoryConfig


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


async def test_bridge_maps_item_and_calls_promote_with_populated_source() -> None:
    """A scored insight becomes a well-formed ``PromotionSource`` handed to ``promote``."""
    from arcagent.modules.memory.promotion import promote_item, to_promotion_source

    item = Insight(
        id="team-knowledge",
        statement="Vendor Acme ships within two weeks of a signed PO.",
        trigger="a lead-time question about Acme",
        classification="unclassified",
        personal_score=3,
    )
    access = KnowledgeAccess(caller_did="did:arc:agent-a", clearance="unclassified")
    port = _FakeSharedPort()

    class _Personal:
        async def export_for_promotion(
            self, reference: str, used_access: KnowledgeAccess
        ) -> PromotionSource:
            assert reference == item.id and used_access == access
            return to_promotion_source(item)

    await promote_item(item, port=port, personal=_Personal(), access=access)

    assert len(port.promoted) == 1
    source, used_access = port.promoted[0]
    assert isinstance(source, PromotionSource)
    # Body carries the insight's own content, classification is preserved verbatim,
    # and the source is attributable back to the origin item + origin DID.
    assert item.statement in source.content
    assert source.classification == "unclassified"
    assert source.title != ""
    assert source.document_type != ""
    assert "team-knowledge" in source.reference.identifier
    assert used_access.caller_did == "did:arc:agent-a"


def test_state_can_hold_a_shared_port(tmp_path: Path) -> None:
    """The memory ``_State`` can carry a shared port (today: no such field)."""
    port = _FakeSharedPort()

    state = _State(
        config=MemoryConfig(),
        brain=NullBrain(),
        workspace=tmp_path,
        telemetry=None,
        bus=None,
        agent_did="did:arc:agent-a",
        active=False,
        shared_knowledge=port,
    )

    assert state.shared_knowledge is port
