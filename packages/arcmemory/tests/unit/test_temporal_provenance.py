"""RED — recall cards carry a WHEN establishment timestamp (SPEC-072 COMP-006).

Drives the REAL ``ArcMemoryBrain`` recall path (no embedder -> BM25 + graph). A
gated recall over a seeded entity card must surface WHEN the underlying memory was
established, so the model can judge staleness (REQ-356). A record with no usable
timestamp degrades to unstamped — it never errors.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Recall, RecallCard

_DID = "did:arc:temporal-agent"


def test_recall_carries_established_field() -> None:
    """The ``Recall``/``RecallCard`` contract exposes an establishment timestamp."""
    # Field must exist on both models (default unstamped, never required).
    assert Recall(source="s", content="c", score=1.0).established == ""
    assert RecallCard(source="s", kind="surface", content="c", score=1.0).established == ""


async def test_recall_cards_surface_establishment_timestamp(workspace: Path) -> None:
    """A gated recall over a seeded entity yields cards stamped with WHEN it was set."""
    brain = ArcMemoryBrain(workspace, _DID)
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=_DID)
    store.write_fact(
        "aurora-index",
        "kind",
        "temporal-candidate",
        name="Aurora Index",
        entity_type="thing",
        classification="unclassified",
    )

    cards = await brain.recall("aurora index", clearance="unclassified")

    assert cards, "expected at least one recall card for the seeded entity"
    stamped = [c for c in cards if c.established]
    assert stamped, "expected at least one card to carry an establishment timestamp"
    # The stamp reads as an ISO calendar date (YYYY-MM-DD prefix) and is rendered
    # into the card's provenance so the model can weigh staleness.
    stamp = stamped[0].established
    assert stamp[:4].isdigit() and stamp[4] == "-"
    assert any(stamp in p for p in stamped[0].provenance)


async def test_recall_render_includes_establishment_timestamp(workspace: Path) -> None:
    """The injectable render surfaces the establishment timestamp (provenance in-band)."""
    brain = ArcMemoryBrain(workspace, _DID)
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=_DID)
    store.write_fact(
        "beacon-relay",
        "kind",
        "temporal-candidate",
        name="Beacon Relay",
        entity_type="thing",
        classification="unclassified",
    )

    text = await brain.retrieve("beacon relay", clearance="unclassified")

    assert text != ""
    assert "established=" in text


def test_absent_timestamp_degrades_to_unstamped_never_errors() -> None:
    """A recall with no usable timestamp is unstamped — the render must not error."""
    from arcmemory.security import render_recalls

    recall = Recall(source="insight:x", content="a minted abstraction", score=0.9)
    assert recall.established == ""
    # Rendering an unstamped recall is fine — no ``established=`` attribute appears.
    rendered = render_recalls([recall])
    assert rendered != ""
    assert "established=" not in rendered
