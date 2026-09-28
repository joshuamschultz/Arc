"""T-1191 (SPEC-083 COMP-021) — ``ConsolidatedMemoryExporter.export_for_promotion``.

RED intent: ``arcmemory.adapters.memory_export`` does not exist yet, so the import
fails with ``ModuleNotFoundError`` — the feature is absent.

Contract under test (SDD COMP-021, README decision 2):

- ``ConsolidatedMemoryExporter(workspace, agent_did, stores)`` where ``stores``
  exposes the three real stores as ``stores.insights`` (``InsightStore``),
  ``stores.procedures`` (``ProceduralStore``) and ``stores.entities``
  (``SemanticStore``).
- ``await export_for_promotion(reference, access)`` for ``insight:<id>``,
  ``procedure:<slug>``, ``entity:<slug>`` re-reads the card FROM DISK, renders it
  via ``render_candidate`` and returns a source with the shape
  ``FleetSharedKnowledgeService.promote`` consumes (same shape as
  ``PersonalKnowledgeAdapter.export_for_promotion``):
  ``reference(scope="personal", identifier=<reference>, digest)``, ``digest``,
  ``content``, ``classification``, ``title``, ``tags``, ``document_type``.
- ``classification == access.clearance`` (the shared store's no-write-down rule
  requires label == writer clearance).
- Refusals: ``access.caller_did != agent_did`` -> ``PermissionError``; a card whose
  own label is not dominated by the clearance -> ``PermissionError``;
  ``episodic:``/``daily:``/unknown kinds, and missing cards, are refused.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from arcmemory.adapters.memory_export import ConsolidatedMemoryExporter
from arcmemory.promotion.render import render_candidate

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Insight, Procedure, Scope, Step

_AGENT = "did:arc:test-agent"
_REFUSED = (PermissionError, ValueError, LookupError)


class _Access:
    def __init__(self, caller_did: str = _AGENT, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = caller_did
        self.clearance = clearance


def _shared_digest(content: str) -> str:
    """The exact formula ``FleetSharedKnowledgeService._validate_promotion`` uses."""
    return "sha256:" + hashlib.sha256(content.strip().encode()).hexdigest()


@pytest.fixture
def stores(workspace: Path, db: MemoryDB, scope: Scope) -> SimpleNamespace:
    return SimpleNamespace(
        insights=InsightStore(workspace),
        procedures=ProceduralStore(workspace),
        entities=SemanticStore(workspace, WeightedGraph(db), scope.key),
    )


@pytest.fixture
def exporter(workspace: Path, stores: SimpleNamespace) -> ConsolidatedMemoryExporter:
    return ConsolidatedMemoryExporter(workspace, _AGENT, stores)


def _write_insight(stores: SimpleNamespace, classification: str = "unclassified") -> Insight:
    insight = Insight(
        id="acme-renewal",
        statement="Acme renewal closes at $42k/yr, net-60.",
        trigger="a renewal negotiation with a named client",
        classification=classification,
    )
    stores.insights.write(insight)
    return insight


@pytest.mark.asyncio
async def test_export_insight_returns_rendered_bytes_at_caller_clearance(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    _write_insight(stores)
    expected = render_candidate(stores.insights.read("acme-renewal"))

    source = await exporter.export_for_promotion("insight:acme-renewal", _Access())

    assert source.reference.scope == "personal"
    assert source.reference.identifier == "insight:acme-renewal"
    assert source.content == expected.content
    assert source.title == expected.title
    assert source.document_type == "insight"
    assert source.digest == expected.content_sha256
    assert source.digest == _shared_digest(source.content)
    assert source.reference.digest == source.digest
    assert source.classification == "UNCLASSIFIED"
    assert isinstance(source.tags, tuple)


@pytest.mark.asyncio
async def test_export_labels_at_clearance_not_at_card_label(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    """Decision 2: the promoted label is the agent's clearance (no write-down)."""
    _write_insight(stores, classification="unclassified")

    source = await exporter.export_for_promotion("insight:acme-renewal", _Access(clearance="CUI"))

    assert source.classification == "CUI"


@pytest.mark.asyncio
async def test_export_procedure_returns_rendered_bytes(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    stores.procedures.write(
        Procedure(
            slug="close-monthly-books",
            title="Close the monthly books",
            when_to_use="At month end, before the board pack is sent.",
            steps=[Step(text="Reconcile the bank feeds"), Step(text="Post accruals")],
        )
    )
    expected = render_candidate(stores.procedures.read("close-monthly-books"))

    source = await exporter.export_for_promotion("procedure:close-monthly-books", _Access())

    assert source.document_type == "procedure"
    assert source.content == expected.content
    assert source.title == "Close the monthly books"
    assert source.digest == _shared_digest(source.content)


@pytest.mark.asyncio
async def test_export_entity_returns_rendered_bytes(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    stores.entities.write_fact("acme-corp", "renewal_price", "$42k/yr", name="Acme Corp")
    expected = render_candidate(stores.entities.read("acme-corp"))

    source = await exporter.export_for_promotion("entity:acme-corp", _Access())

    assert source.document_type == "entity"
    assert source.content == expected.content
    assert "renewal_price: $42k/yr" in source.content
    assert source.title == "Acme Corp"
    assert source.digest == _shared_digest(source.content)


@pytest.mark.asyncio
async def test_export_rereads_disk_so_an_edit_changes_the_digest(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    """The shared side re-verifies bytes; the exporter must serve what is on disk now."""
    _write_insight(stores)
    before = await exporter.export_for_promotion("insight:acme-renewal", _Access())
    stores.insights.write(
        Insight(
            id="acme-renewal",
            statement="Acme renewal closes at $45k/yr, net-30.",
            trigger="a renewal negotiation with a named client",
        )
    )

    after = await exporter.export_for_promotion("insight:acme-renewal", _Access())

    assert after.content == "Acme renewal closes at $45k/yr, net-30."
    assert after.digest != before.digest


@pytest.mark.asyncio
async def test_export_refuses_a_foreign_caller_did(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    """ASI03: another agent cannot pull this agent's memory across DID isolation."""
    _write_insight(stores)

    with pytest.raises(PermissionError):
        await exporter.export_for_promotion(
            "insight:acme-renewal", _Access(caller_did="did:arc:other-agent")
        )


@pytest.mark.asyncio
async def test_export_refuses_a_card_labelled_above_clearance(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    """A SECRET card is never laundered down to a CUI shared label."""
    _write_insight(stores, classification="secret")

    with pytest.raises(PermissionError):
        await exporter.export_for_promotion("insight:acme-renewal", _Access(clearance="CUI"))


@pytest.mark.asyncio
async def test_export_allows_a_card_at_exactly_clearance(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace
) -> None:
    """Narrowness pair: equal label and clearance is dominated and exports."""
    _write_insight(stores, classification="cui")

    source = await exporter.export_for_promotion("insight:acme-renewal", _Access(clearance="CUI"))

    assert source.classification == "CUI"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "episodic:evt-1",
        "daily:2026-09-26",
        "note:acme-renewal",
        "acme-renewal",
        "",
    ],
)
async def test_export_refuses_non_promotable_references(
    exporter: ConsolidatedMemoryExporter, stores: SimpleNamespace, reference: str
) -> None:
    """Only insight/procedure/entity references are exportable (REQ-486)."""
    _write_insight(stores)

    with pytest.raises(_REFUSED):
        await exporter.export_for_promotion(reference, _Access())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    ["insight:does-not-exist", "procedure:does-not-exist", "entity:does-not-exist"],
)
async def test_export_refuses_a_missing_card(
    exporter: ConsolidatedMemoryExporter, reference: str
) -> None:
    """A missing card never yields an empty or fabricated source."""
    with pytest.raises(_REFUSED):
        await exporter.export_for_promotion(reference, _Access())


@pytest.mark.asyncio
async def test_export_refuses_path_traversal_reference(
    exporter: ConsolidatedMemoryExporter, workspace: Path
) -> None:
    """A traversal-shaped id never reads a file outside the insight store."""
    outside = workspace / "memory" / "stolen.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("---\nid: stolen\n---\n## Statement\nexfil\n", encoding="utf-8")

    with pytest.raises(_REFUSED):
        await exporter.export_for_promotion("insight:../stolen", _Access())
