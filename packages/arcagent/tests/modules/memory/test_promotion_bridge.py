"""T-1124 (SPEC-083 COMP-005) — the promotion bridge + a shared port on ``_State``.

RED intent, two parts:

1. A bridge maps a scored ``Insight``/``Procedure``/``Entity`` to a
   :class:`arcagent.knowledge.PromotionSource` (title / body / document_type /
   classification / origin DID / provenance) and calls
   :meth:`arcagent.knowledge.SharedKnowledgePort.promote`. It fails today because
   the bridge module does not exist (``ModuleNotFoundError`` on import).
2. The memory module ``_State`` can hold a shared port. It fails today because
   ``_State`` has no ``shared_knowledge`` field (``TypeError`` on construction).

Where the bridge belongs (stated for the GREEN implementer, T-1125):
``PromotionSource`` and ``SharedKnowledgePort`` live in ``arcagent.knowledge`` and
``arcmemory`` may NOT import ``arcagent`` (the DAG leaf rule, enforced by
``arcmemory/tests/architecture/test_no_arcagent_import.py``). So the bridge that
constructs a ``PromotionSource`` and calls ``SharedKnowledgePort.promote`` MUST be
arcagent-side. Intended home: ``arcagent.modules.memory.promotion`` — the one
layer that legally knows both the arcmemory item types (optional extra) and the
``arcagent.knowledge`` ports.

Intended API:

- ``arcagent.modules.memory.promotion.to_promotion_source(item) -> PromotionSource``
  — title from the item, content carrying the item body, ``classification`` from
  the item, a ``KnowledgeRef(scope="personal", identifier=<item id/slug>, ...)``.
- ``async promote_item(item, *, port, access) -> KnowledgeRef`` — build the source
  and call ``port.promote(source, access)``; origin DID travels on
  ``access.caller_did``.
- ``_State`` gains ``shared_knowledge: SharedKnowledgePort | None = None`` (mirrors
  the existing ``personal_knowledge`` field), attached out-of-band at fleet-serve.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcagent.brain import NullBrain
from arcagent.knowledge import KnowledgeAccess, KnowledgeRef, PromotionSource
from arcagent.modules.memory._runtime import _State
from arcagent.modules.memory.config import MemoryConfig
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


async def test_bridge_maps_item_and_calls_promote_with_populated_source() -> None:
    """A scored insight becomes a well-formed ``PromotionSource`` handed to ``promote``."""
    from arcagent.modules.memory.promotion import promote_item

    item = Insight(
        id="team-knowledge",
        statement="Vendor Acme ships within two weeks of a signed PO.",
        trigger="a lead-time question about Acme",
        classification="unclassified",
        personal_score=3,
    )
    access = KnowledgeAccess(caller_did="did:arc:agent-a", clearance="unclassified")
    port = _FakeSharedPort()

    await promote_item(item, port=port, access=access)

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
