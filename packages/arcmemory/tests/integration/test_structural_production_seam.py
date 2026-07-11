"""F2 — the analogical centerpiece reaches the PRODUCTION seam (not just a unit rig).

The Phase-8/9 seam built ``Situation(text=query, summary="", cues=[])`` inside
``ArcMemoryBrain.retrieve`` — so the abstraction never reached the structural channel:
the cue channel could only fire when the raw query literally *contained* an insight's
cue phrase. The old AC-6 test masked this by planting that very phrase in the probe.

These tests drive the *production* ``ArcMemoryBrain.retrieve(query, summary=…, cues=…)``
with a present situation from a DIFFERENT DOMAIN that shares **zero salient tokens** with
the insight's TRIGGER and CUES (and its episodes). The only bridges left are structural:

* channel (a) trigger-embedding — the reused turn *summary* names the same mechanism with
  DIFFERENT tokens that a real embedder maps to the same abstraction dimension; and
* channel (b) cue-graph spreading — the turn's active concept node reaches the insight
  through a **learned graph edge**, never through a shared cue token.

A negative test proves the cue channel is genuinely graph-mediated: remove the learned
bridge edge and the same zero-overlap situation no longer matches (so the positive result
was the graph doing the work, not lexical leakage or a rigged same-cue-phrase probe).
"""

from __future__ import annotations

from pathlib import Path

from arctrust.classification import Classification

from arcmemory.brain import ArcMemoryBrain
from arcmemory.db import DEFAULT_DIMS, MemoryDB
from arcmemory.distill import (
    DaySummaryDraft,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.structural import StructuralIndex
from arcmemory.retrieve import Retriever
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope, Situation

_DID = "did:arc:analogy-agent"

# The insight's abstraction (dim 4) and a DISTINCT summary phrasing (dim 4 via different
# tokens). No token is shared between the two sets, yet both hit the same dimension — so
# the trigger-embedding match is by *structure*, never by a shared surface token.
_TRIGGER = "a property is asserted yet the enforcing mechanism stays unwired"
_TRIGGER_MARKERS = {"asserted", "unwired"}
_SUMMARY_MARKERS = {"declared", "uninvoked"}
_INSIGHT_CUE = "claims-without-enforcement"
_INSIGHT_STATEMENT = "A guarantee is claimed but its enforcement is never connected."

# The turn's active concept node (domain B) — bridged to the insight's cue by a learned
# edge, NOT by a shared token.
_FOREIGN_CUE = "settlement-ceiling"

# Planted past episodes (domain A: kitchen / ops). None carries a mechanism marker.
_EPISODES = [
    "the recipe lists salt but the cook forgets it",
    "the checklist names a valve the operator skips",
    "the manifest names a step nobody performs",
]

# Present situation (domain B: finance). Zero mechanism markers in the query; the summary
# names the mechanism with the dim-4 markers "declared"/"uninvoked".
_QUERY = "the settlement ledger overran its posted ceiling"
_SUMMARY = "a safeguard is declared but left uninvoked on the live path"


class ConceptEmbedder:
    """Deterministic 384-dim embedder: a mechanism marker -> abstraction dimension 4."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vec = [0.0] * DEFAULT_DIMS
            if any(w in lowered for w in _TRIGGER_MARKERS | _SUMMARY_MARKERS):
                vec[4] = 1.0
            out.append(vec)
        return out


class PlantingDistiller:
    """Mints the probe insight, stated purely in abstraction space (dim-4 trigger)."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint(
            insights=[
                InsightCandidate(
                    id="silent-noop",
                    statement=_INSIGHT_STATEMENT,
                    trigger=_TRIGGER,
                    cues=[_INSIGHT_CUE],
                    instances=[e.event_id for e in events],
                )
            ]
        )

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()


def _salient(text: str) -> set[str]:
    return {t for t in text.lower().replace("-", " ").split() if len(t) > 3}


def _assert_zero_overlap_vs_insight(*texts: str) -> None:
    """No probe text shares a salient token with the insight's TRIGGER, CUES, or episodes."""
    insight_tokens = _salient(_TRIGGER) | _salient(_INSIGHT_CUE)
    episode_tokens: set[str] = set()
    for ep in _EPISODES:
        episode_tokens |= _salient(ep)
    forbidden = insight_tokens | episode_tokens
    for text in texts:
        clash = _salient(text) & forbidden
        assert not clash, f"{text!r} leaked salient token(s) {clash} into the structural probe"


async def _plant_probe(workspace: Path, *, bridge: bool) -> ArcMemoryBrain:
    """Seed episodes, mint the insight, and (optionally) plant the learned bridge edge."""
    scope = Scope(agent_did=_DID)
    seed_db = MemoryDB(workspace)
    seed_db.connect()
    episodic = EpisodicStore(seed_db, workspace)
    for i, text in enumerate(_EPISODES):
        ev = Event(
            event_id=f"e{i}", scope=scope.key, kind="obs", text=text, ts=f"2026-01-01T00:00:0{i}Z"
        )
        episodic.append(ev)

    brain = ArcMemoryBrain(
        workspace, _DID, embedder=ConceptEmbedder(), distiller=PlantingDistiller()
    )
    await brain.consolidate()  # mint the abstraction offline

    if bridge:
        # A LEARNED cross-domain edge: the finance concept associates with the abstract
        # cue. This is the structural mapping — spreading activation traverses it.
        WeightedGraph(MemoryDB(workspace)).hebbian_bump(scope.key, _FOREIGN_CUE, _INSIGHT_CUE)
    return brain


async def _structural(workspace: Path) -> StructuralIndex:
    """A production structural index over the brain's own DB, trigger-embedded."""
    idx = StructuralIndex(
        MemoryDB(workspace), workspace, Scope(agent_did=_DID), embedder=ConceptEmbedder()
    )
    await idx.trigger_index()
    return idx


def _situation() -> Situation:
    return Situation(text=_QUERY, summary=_SUMMARY, cues=[_FOREIGN_CUE])


async def test_production_retrieve_matches_zero_overlap_probe(workspace: Path) -> None:
    """End-to-end: the production ``brain.retrieve`` surfaces the zero-overlap probe."""
    # The probe shares NO salient token with the insight's trigger + cues + episodes:
    # the only bridges are the dim-4 abstraction and the learned graph edge.
    _assert_zero_overlap_vs_insight(_QUERY, _SUMMARY, _FOREIGN_CUE)
    assert not (_SUMMARY_MARKERS & _TRIGGER_MARKERS), "summary/trigger must not share a marker"

    brain = await _plant_probe(workspace, bridge=True)
    text = await brain.retrieve(
        _QUERY, summary=_SUMMARY, cues=[_FOREIGN_CUE], top_k=5, budget=10_000
    )
    assert "enforcement is never connected" in text


async def test_production_retrieve_enriches_the_structural_recall(workspace: Path) -> None:
    """The SDD-7 payoff on the REAL path: the spotted insight arrives *enriched*.

    Driving the production ``Retriever.retrieve`` (surface + structural fused, gated,
    bounded), the structural recall's agent-visible content must carry the enriched
    neighborhood — an original instance episode — not the bare statement. This is the
    end-to-end proof that ``enrich`` is wired into the return path, not producer-only.
    """
    _assert_zero_overlap_vs_insight(_QUERY, _SUMMARY, _FOREIGN_CUE)
    await _plant_probe(workspace, bridge=True)
    scope = Scope(agent_did=_DID)
    rv = Retriever(MemoryDB(workspace), workspace, scope, embedder=ConceptEmbedder())
    await rv.index()

    bundle = await rv.retrieve(
        _situation(), clearance=Classification.UNCLASSIFIED, top_k=5, budget=10_000
    )
    structural = [r for r in bundle.recalls if r.source == "silent-noop"]
    assert structural, "the structural channel must contribute the spotted insight"
    content = structural[0].content
    assert _INSIGHT_STATEMENT in content  # the abstraction itself
    # ...anchored to an ORIGINAL instance episode (the enrichment reached the agent).
    assert any(ep in content for ep in _EPISODES), "structural recall must be enriched, not bare"


async def test_both_structural_channels_fire_via_abstraction_and_graph(workspace: Path) -> None:
    """The clean proof: BOTH channels promote the probe — trigger by abstraction, cue by graph."""
    _assert_zero_overlap_vs_insight(_QUERY, _SUMMARY, _FOREIGN_CUE)
    await _plant_probe(workspace, bridge=True)
    idx = await _structural(workspace)
    situation = _situation()

    # (a) trigger-embedding: the summary's dim-4 markers ("declared"/"uninvoked") match
    # the trigger's dim-4 markers ("asserted"/"unwired") — different tokens, same dim.
    trig = await idx.trigger_match(situation, top_k=5)
    assert trig is not None and trig[0][0] == "silent-noop"

    # (b) cue-graph: the foreign concept reaches the insight ONLY through the learned edge.
    cue = idx.cue_match(situation, top_k=5)
    assert cue and cue[0][0] == "silent-noop"

    # Conjunctive gating promotes the probe (both channels agreed).
    matched = await idx.match(situation, top_k=5)
    assert matched.recalls and matched.recalls[0].source == "silent-noop"
    assert matched.degraded is False


async def test_cue_channel_is_graph_mediated_not_lexical(workspace: Path) -> None:
    """Negative control: remove the learned edge and the cue channel goes empty.

    The trigger channel still fires (the abstraction is unchanged), but with no graph
    path the cue channel yields nothing and conjunctive gating drops the candidate — so
    the positive match was the GRAPH doing the work, never a shared cue token.
    """
    await _plant_probe(workspace, bridge=False)
    idx = await _structural(workspace)
    situation = _situation()

    assert (await idx.trigger_match(situation, top_k=5))[0][0] == "silent-noop"  # type: ignore[index]
    assert idx.cue_match(situation, top_k=5) == [], "no graph edge -> no cue activation"
    assert (await idx.match(situation, top_k=5)).recalls == [], "conjunctive gating drops it"
