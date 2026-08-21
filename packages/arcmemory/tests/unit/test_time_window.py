"""RED — optional TimeWindow filter on recall (SPEC-072 COMP-008).

Recall accepts an optional ``TimeWindow`` that filters candidates to the window before
the bound; ``None`` is a no-op (today's behavior). A stamped card outside the window is
dropped; an unstamped card is kept (it can't be filtered, so don't over-drop). REQ-358,
deterministic — no LLM/embedder on the filter itself.
"""

from __future__ import annotations

from typing import Any

from arctrust.classification import Classification
from packages.arcmemory.tests.conftest import StubEmbedder

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.retrieve import Retriever, _within_window
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Recall, Scope, Situation, TimeWindow

_DID = "did:arc:time-window-agent"


def test_within_window_filters_stamped_out_of_window_keeps_unstamped() -> None:
    in_win = Recall(source="a", content="x", score=1.0, established="2026-08-20")
    out_win = Recall(source="b", content="y", score=0.9, established="2000-01-01")
    unstamped = Recall(source="c", content="z", score=0.8, established="")
    window = TimeWindow(start="2026-08-01", end="2026-08-31")

    kept = {r.source for r in _within_window([in_win, out_win, unstamped], window)}
    assert "a" in kept  # inside the window
    assert "b" not in kept  # outside → dropped
    assert "c" in kept  # unstamped → kept (cannot be filtered)


def test_within_window_none_is_a_noop() -> None:
    recalls = [Recall(source="a", content="x", score=1.0, established="2000-01-01")]
    assert _within_window(recalls, None) == recalls


async def test_retrieve_threads_window_and_filters(
    workspace: Any, db: MemoryDB, embedder: StubEmbedder
) -> None:
    scope = Scope(agent_did=_DID)
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("comet-probe", "kind", "candidate", name="Comet Probe", entity_type="thing")

    retr = Retriever(db, workspace, scope, config=MemoryConfig(), embedder=embedder)
    await retr.index()

    open_cards = await retr.recall_cards(
        Situation(text="comet probe"), clearance=Classification.UNCLASSIFIED, top_k=5
    )
    assert open_cards, "no window: the freshly-stamped card should surface"

    past = TimeWindow(start="2000-01-01", end="2000-12-31")
    past_cards = await retr.recall_cards(
        Situation(text="comet probe"),
        clearance=Classification.UNCLASSIFIED,
        top_k=5,
        window=past,
    )
    assert not any(c.source == open_cards[0].source for c in past_cards)
