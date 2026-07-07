"""T-040/041/042 — surface retrieval: incremental index, RRF fusion, degrade.

The embeddings here are a deterministic *concept* embedder (not the sha256 stub):
it maps each text to a small concept-space vector via a synonym lexicon, so two
texts that share a concept but **no surface tokens** land near each other. That is
what lets these tests prove semantic recall (embeddings, not substrings) without a
real model or a network call.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.audit import AuditEvent

from arcmemory.db import MemoryDB
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope

# Concept lexicon: distinct synonym sets map to distinct concept dimensions.
# "dog"/"puppy"/"canine" -> dim 0; "cat"/"kitten"/"feline" -> dim 1; etc. No two
# rows share a *token*, but rows in the same concept share a *dimension*.
_CONCEPTS: dict[int, set[str]] = {
    0: {"dog", "puppy", "canine", "hound", "barked"},
    1: {"cat", "kitten", "feline", "meowed"},
    2: {"car", "sedan", "automobile", "engine"},
    3: {"boat", "vessel", "ship", "sailed"},
}
# Pad to the test DB's vec0 width (conftest opens MemoryDB at dims=8).
_DIMS = 8


class ConceptEmbedder:
    """Deterministic semantic embedder over a fixed concept lexicon."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vec = [0.0] * _DIMS
            for dim, words in _CONCEPTS.items():
                if any(w in lowered for w in words):
                    vec[dim] = 1.0
            out.append(vec)
        return out


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _append(episodic: EpisodicStore, scope: Scope, eid: str, text: str, ts: str) -> None:
    episodic.append(Event(event_id=eid, ts=ts, scope=scope.key, kind="obs", text=text))


def _surface(
    db: MemoryDB, workspace: Path, scope: Scope, *, embedder=None, sink=None
) -> SurfaceIndex:
    return SurfaceIndex(db, workspace, scope, embedder=embedder, audit_sink=sink)


# -- T-040: incremental, content-gated indexing -----------------------------


def test_index_embeds_all_then_skips_unchanged(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "the puppy barked", "2026-07-07T00:00:00+00:00")
    _append(episodic, scope, "e1", "the kitten meowed", "2026-07-07T00:00:01+00:00")

    surface = _surface(db, workspace, scope, embedder=emb)
    first = surface.index_if_needed()
    assert first == 2  # both event chunks embedded on first pass
    assert emb.calls == 2

    # Nothing changed -> no re-embedding on the second pass.
    second = surface.index_if_needed()
    assert second == 0
    assert emb.calls == 2


def test_index_reembeds_only_changed_file(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    entities = workspace / "memory" / "entities"
    entities.mkdir(parents=True)
    (entities / "rex.md").write_text("---\nname: Rex\n---\n\nthe hound barked")
    (entities / "felix.md").write_text("---\nname: Felix\n---\n\nthe feline meowed")

    surface = _surface(db, workspace, scope, embedder=emb)
    surface.index_if_needed()
    baseline = emb.calls
    assert baseline == 2

    # Change one file's *content* -> only that chunk re-embeds.
    (entities / "rex.md").write_text("---\nname: Rex\n---\n\nthe canine sailed")
    changed = surface.index_if_needed()
    assert changed == 1
    assert emb.calls == baseline + 1


# -- T-041: RRF fusion, semantic recall, recency ----------------------------


def test_semantic_query_with_no_lexical_overlap_is_recalled(workspace, db, scope) -> None:
    """A query sharing a CONCEPT but no TOKEN with the target still retrieves it."""
    emb = ConceptEmbedder()
    episodic = EpisodicStore(db, workspace)
    # Target shares the "canine" concept with the query but zero surface tokens.
    _append(episodic, scope, "e0", "the hound sailed away", "2026-07-07T00:00:00+00:00")
    # Distractors in unrelated concepts.
    _append(episodic, scope, "e1", "the kitten meowed loudly", "2026-07-07T00:00:01+00:00")
    _append(episodic, scope, "e2", "the sedan needs an engine", "2026-07-07T00:00:02+00:00")

    surface = _surface(db, workspace, scope, embedder=emb)
    surface.index_if_needed()

    result = surface.search("a small puppy", top_k=1)
    assert not result.degraded
    assert result.recalls, "expected a recall"
    top = result.recalls[0]
    assert "hound sailed" in top.content  # the canine-concept chunk, no shared token
    assert "puppy" not in top.content  # proves it was NOT a substring match


def test_fusion_beats_bm25_alone(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "the hound sailed away", "2026-07-07T00:00:00+00:00")
    _append(episodic, scope, "e1", "the kitten meowed loudly", "2026-07-07T00:00:01+00:00")

    surface = _surface(db, workspace, scope, embedder=emb)
    surface.index_if_needed()

    # BM25 alone finds nothing (no shared token with "puppy").
    bm25_only = surface.bm25_only("a small puppy", top_k=2)
    assert all("hound" not in c for c in bm25_only)
    # Fusion (with the concept vector) surfaces the canine chunk.
    fused = surface.search("a small puppy", top_k=2)
    assert any("hound sailed" in r.content for r in fused.recalls)


def test_newer_chunk_ranks_higher_on_a_tie(workspace, db, scope) -> None:
    emb = ConceptEmbedder()
    episodic = EpisodicStore(db, workspace)
    # Two lexically-identical chunks; only the timestamp differs.
    _append(episodic, scope, "old", "the puppy barked", "2026-07-01T00:00:00+00:00")
    _append(episodic, scope, "new", "the puppy barked", "2026-07-07T00:00:00+00:00")

    surface = _surface(db, workspace, scope, embedder=emb)
    surface.index_if_needed()

    result = surface.search("the puppy barked", top_k=2)
    ordered = [r.source for r in result.recalls]
    assert ordered.index("event:new") < ordered.index("event:old")


# -- T-042: degrade to BM25 + graph when embeddings are unavailable ---------


def test_degrades_to_bm25_when_embedder_absent(workspace, db, scope) -> None:
    sink = RecordingSink()
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "the puppy barked", "2026-07-07T00:00:00+00:00")

    surface = _surface(db, workspace, scope, embedder=None, sink=sink)
    surface.index_if_needed()  # no embedder -> no vectors, fts still built

    result = surface.search("puppy", top_k=3)
    # BM25 still returns the lexical match; no exception is raised.
    assert result.degraded
    assert any("puppy" in r.content for r in result.recalls)
    assert any(e.action == "recall.degraded" for e in sink.events)
