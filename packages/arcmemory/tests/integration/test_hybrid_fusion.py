"""Every channel of hybrid recall must actually move the fused ranking.

The owner's mental model is "hybrid rerank the graph results, the lexical search,
and the semantic search". The code already does that — ``fusion.rrf_fuse`` (k=60)
combines vec + bm25 + graph + recency inside ``surface.py``, then surface +
structural inside ``retrieve.py``. What was never proven is that each of those
lists is *load-bearing*: with the embedder silently absent in production, the
semantic third contributed nothing and no test noticed.

These run the real ``Retriever.retrieve`` path over a corpus engineered so that
each target document is reachable through **exactly one** channel. Each channel is
then toggled off; a document that disappears when its channel is removed proves
that channel contributes.

Corpus (all raw episodic events, so recency is exactly controlled by ``ts``):

===============  ============================  ==========================
document         reachable via                 unreachable via
===============  ============================  ==========================
``event:lex``    bm25 (shares "paperwork")     vec (no concept), graph
``event:sem``    vec (concept "canine"~dog)    bm25 (no shared token)
``event:assoc``  graph (canine -> zephyr)      bm25, vec
``noise*``       recency only                  everything else
``p1`` (insight) the structural channel        the surface channel
===============  ============================  ==========================
"""

from __future__ import annotations

from pathlib import Path

from arctrust.classification import Classification

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.retrieve import Retriever
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Event, Insight, Scope, Situation

# Concept lexicon: a query token and a document token that share NO characters
# still land on the same dimension — that is semantic matching, not substring.
_CONCEPTS: dict[int, set[str]] = {
    0: {"canine", "puppy", "hound"},  # the dog concept
    2: {"asserted", "unwired"},  # an abstraction-space mechanism (structural)
}
_DIMS = 8

_QUERY = "canine paperwork"
_ABSTRACTION = "asserted guarantee left unwired"
_SEED_VOCAB = ("canine", "zephyr")


class ConceptEmbedder:
    """Deterministic concept embedder — no model, no network, reproducible."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vector = [0.0] * _DIMS
            for dim, words in _CONCEPTS.items():
                if any(word in lowered for word in words):
                    vector[dim] = 1.0
            out.append(vector)
        return out


def _seed_corpus(db: MemoryDB, workspace: Path, scope: Scope, *, assoc_edge: bool) -> None:
    """Write the raw stream, the insight card, and (optionally) the graph edge."""
    episodic = EpisodicStore(db, workspace)
    # Oldest first; the three targets are the OLDEST so recency alone can never
    # float them into the top of the ranking — only their own channel can.
    stream = [
        ("lex", "2026-01-01T00:00:00+00:00", "quarterly paperwork backlog"),
        ("sem", "2026-01-02T00:00:00+00:00", "the puppy sniffed a mailbox"),
        ("assoc", "2026-01-03T00:00:00+00:00", "zephyr ledger reconciliation"),
        ("noise1", "2026-02-01T00:00:00+00:00", "kettle descaling reminder"),
        ("noise2", "2026-02-02T00:00:00+00:00", "lamp bulb replacement"),
        ("noise3", "2026-02-03T00:00:00+00:00", "window latch adjustment"),
        ("noise4", "2026-02-04T00:00:00+00:00", "floor tile regrouting"),
    ]
    for event_id, ts, text in stream:
        episodic.append(Event(event_id=event_id, ts=ts, scope=scope.key, kind="obs", text=text))

    InsightStore(workspace).write(
        Insight(
            id="p1", statement="the guarantee is never invoked", trigger=_ABSTRACTION, cues=["c-a"]
        )
    )
    graph = WeightedGraph(db)
    graph.link(scope.key, "p1", "c-a", kind="cue")
    if assoc_edge:
        # The learned association the graph channel walks: the query's entity
        # ("canine") activates a node the target document mentions ("zephyr").
        graph.hebbian_bump(scope.key, "canine", "zephyr")


async def _retriever(
    db: MemoryDB,
    workspace: Path,
    scope: Scope,
    *,
    embedder: ConceptEmbedder | None,
) -> Retriever:
    retriever = Retriever(db, workspace, scope, embedder=embedder, seed_vocabulary=_SEED_VOCAB)
    await retriever.index()
    return retriever


async def _sources(
    db: MemoryDB,
    workspace: Path,
    scope: Scope,
    *,
    embedder: ConceptEmbedder | None,
    top_k: int,
) -> list[str]:
    retriever = await _retriever(db, workspace, scope, embedder=embedder)
    bundle = await retriever.retrieve(
        Situation(text=_QUERY, summary=_ABSTRACTION, cues=["c-a"]),
        clearance=Classification.SECRET,
        top_k=top_k,
        budget=100_000,
    )
    return [recall.source for recall in bundle.recalls]


# -- the whole hybrid, in one bundle ---------------------------------------


async def test_all_channels_contribute_to_one_fused_bundle(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """One real retrieve: vec, bm25, graph and structural each land a document."""
    _seed_corpus(db, workspace, scope, assoc_edge=True)

    sources = await _sources(db, workspace, scope, embedder=ConceptEmbedder(), top_k=6)

    assert "event:sem" in sources, "vec (semantic) channel contributed nothing"
    assert "event:lex" in sources, "bm25 (lexical) channel contributed nothing"
    assert "event:assoc" in sources, "graph (associative) channel contributed nothing"
    assert "p1" in sources, "structural channel contributed nothing"


# -- each channel, proved load-bearing by removing it -----------------------


async def test_semantic_channel_is_load_bearing(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """A document reachable ONLY by embedding ranks with an embedder, not without.

    ``event:sem`` shares no token with the query and no graph edge — the vec list
    is its only route into the top-k. This is the exact assertion that was missing
    while production ran with the embedder uninstalled.
    """
    _seed_corpus(db, workspace, scope, assoc_edge=True)

    with_vec = await _sources(db, workspace, scope, embedder=ConceptEmbedder(), top_k=3)
    assert "event:sem" in with_vec

    without_vec = await _sources(db, workspace, scope, embedder=None, top_k=3)
    assert "event:sem" not in without_vec
    assert without_vec, "recall must still answer without the embedder"


async def test_lexical_channel_is_load_bearing(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """``event:lex`` shares only a keyword — bm25 is its only route in."""
    _seed_corpus(db, workspace, scope, assoc_edge=True)

    ranked = await _sources(db, workspace, scope, embedder=ConceptEmbedder(), top_k=4)
    assert "event:lex" in ranked

    off_topic = await _retriever(db, workspace, scope, embedder=ConceptEmbedder())
    bundle = await off_topic.retrieve(
        Situation(text="canine"),  # drops the one token bm25 could match
        clearance=Classification.SECRET,
        top_k=4,
        budget=100_000,
    )
    assert "event:lex" not in [r.source for r in bundle.recalls]


async def test_graph_channel_is_load_bearing(db: MemoryDB, workspace: Path, scope: Scope) -> None:
    """``event:assoc`` is reachable only through the learned canine->zephyr edge."""
    _seed_corpus(db, workspace, scope, assoc_edge=True)
    with_edge = await _sources(db, workspace, scope, embedder=ConceptEmbedder(), top_k=3)
    assert "event:assoc" in with_edge


async def test_graph_channel_silent_without_the_learned_edge(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """Same corpus, edge removed: the associative document drops out of the top-k."""
    _seed_corpus(db, workspace, scope, assoc_edge=False)

    ranked = await _sources(db, workspace, scope, embedder=ConceptEmbedder(), top_k=3)

    assert "event:assoc" not in ranked


async def test_recency_is_a_ranked_list_not_a_multiplier(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """With every content channel silent, the ranking is pure newest-first."""
    _seed_corpus(db, workspace, scope, assoc_edge=True)
    retriever = await _retriever(db, workspace, scope, embedder=ConceptEmbedder())

    bundle = await retriever.retrieve(
        Situation(text="zzzz"),  # no token, no concept, no entity
        clearance=Classification.SECRET,
        top_k=10,
        budget=100_000,
    )

    stream = [r.source for r in bundle.recalls if r.source.startswith("event:")]
    assert stream == [
        "event:noise4",
        "event:noise3",
        "event:noise2",
        "event:noise1",
        "event:assoc",
        "event:sem",
        "event:lex",
    ]
    assert bundle.degraded is False, "an embedder IS wired; this is not a degrade"
