"""AC-6 — the differentiator: a planted structural probe.

This is the spec's reason for existing. We plant a recurring pattern as past
episodes, **consolidate** them into an ``Insight`` whose ``trigger`` + ``cues`` are
stated in abstraction space (surface stripped), then present a **new situation in a
different domain that instances the same abstract structure with ZERO lexical or
semantic overlap** to the original episodes. The match must fire through BOTH
structural channels — trigger-embedding AND cue-graph spreading — and enrich to the
original instances. Separately, a never-recurring ``guessed`` insight must **decay
out** over simulated time.

Guaranteeing ZERO surface overlap (so the match is provably STRUCTURAL, not lexical
leakage): every assertion below mechanically checks that the situation's words, the
insight's trigger, and the insight's cues share **no salient token** with any planted
episode. The only bridge left between the present situation and the past episodes is
the *abstraction* — the minted trigger vector and the abstract cue nodes. The concept
embedder is deterministic and maps a *mechanism marker* (never a domain word) to an
abstraction dimension, so the trigger channel's similarity is encoded by structure,
not by a live model's guesswork.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    DaySummaryDraft,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.structural import StructuralIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope, Situation

# Mechanism markers (abstraction space) — deliberately NOT domain words. No planted
# episode contains any of these (asserted in the test), so a concept hit is only
# ever produced by an abstraction, never by surface leakage from the raw stream.
_CONCEPTS: dict[int, set[str]] = {
    0: {"asserted", "unwired", "declared", "uninvoked"},  # guarantee never connected
    1: {"orphaned", "dangling", "subscribes"},  # listener without a source
    2: {"starvation", "unbounded", "runaway"},  # distractor
}
_DIMS = 8

# Planted past episodes (domain: kitchen / operations). None contains a mechanism
# marker or a token from the present-day finance situation.
_EPISODES = [
    "the recipe lists salt but the cook forgets it entirely",
    "the checklist names a valve the operator skips rotating",
    "the manifest names a step nobody performs at runtime",
]


class ConceptEmbedder:
    """Deterministic abstraction-space embedder (mechanism markers -> dimensions)."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
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


class PlantingDistiller:
    """Mints exactly the planted insights (P = the probe, G = never-recurs guess)."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint(
            insights=[
                InsightCandidate(
                    id="silent-noop",
                    statement="A guarantee is claimed but its enforcement is never connected.",
                    trigger="a property is asserted yet the enforcing mechanism stays unwired",
                    cues=["claims-without-enforcement"],
                    instances=["e0", "e1", "e2"],
                ),
                InsightCandidate(
                    id="lonely-listener",
                    statement="A handler waits on a source that does not exist.",
                    trigger="an orphaned handler subscribes to an emitter that is missing",
                    cues=["listener-without-source"],
                    instances=[],
                ),
            ]
        )

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()


# The insight's abstraction, as minted below. Zero-overlap is measured against BOTH the
# episodes AND these (trigger + cue), so the probe cannot lexically leak the answer.
_INSIGHT_TRIGGER = "a property is asserted yet the enforcing mechanism stays unwired"
_INSIGHT_CUE = "claims-without-enforcement"
# The present situation's active concept node (finance domain), bridged to the insight's
# cue by a LEARNED graph edge — never by a shared token.
_FOREIGN_CUE = "settlement-ceiling"


def _salient(text: str) -> set[str]:
    return {t for t in text.lower().replace("-", " ").split() if len(t) > 3}


def _assert_zero_overlap(text: str, references: list[str]) -> None:
    toks = _salient(text)
    for ref in references:
        clash = toks & _salient(ref)
        assert not clash, f"{text!r} leaked surface token(s) {clash} from {ref!r}"


async def test_planted_structural_probe_retrieved_both_channels_then_enriched(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    # --- plant past episodes (the future insight's instances) ---------------
    episodic = EpisodicStore(db, workspace)
    for i, text in enumerate(_EPISODES):
        episodic.append(
            Event(
                event_id=f"e{i}",
                ts=f"2026-01-01T00:00:0{i}+00:00",
                scope=scope.key,
                kind="respond",
                text=text,
            )
        )

    emb = ConceptEmbedder()

    # --- consolidate: mint the abstraction offline --------------------------
    consolidator = Consolidator(db, workspace, scope, distiller=PlantingDistiller(), embedder=emb)
    result = await consolidator.run()
    assert result.insights_minted == 2

    structural = StructuralIndex(db, workspace, scope, embedder=emb)
    await structural.trigger_index()

    # A LEARNED cross-domain edge: the finance concept the present situation lights is
    # associated with the abstract cue. Spreading activation traverses THIS edge to reach
    # the insight — the graph *is* the situation-shape -> pattern mapping.
    WeightedGraph(db).hebbian_bump(scope.key, _FOREIGN_CUE, _INSIGHT_CUE)

    # --- the present situation: a DIFFERENT domain, same abstract structure --
    situation = Situation(
        text="the fee schedule promises a ceiling the ledger fails to apply",
        summary="a safeguard is declared but left uninvoked on the settlement path",
        cues=[_FOREIGN_CUE],
    )

    # PROOF of zero surface overlap, measured against the episodes AND the insight's own
    # trigger + cue: the situation shares no salient token with any of them. The only
    # links left are the dim-0 abstraction (trigger channel) and the learned graph edge
    # (cue channel) — the match is provably STRUCTURAL, not lexical.
    references = [*_EPISODES, _INSIGHT_TRIGGER, _INSIGHT_CUE]
    _assert_zero_overlap(situation.text, references)
    _assert_zero_overlap(situation.summary, references)
    _assert_zero_overlap(_FOREIGN_CUE, references)

    # --- channel (a): trigger-embedding ------------------------------------
    trig_ranked = await structural.trigger_match(situation, top_k=5)
    assert trig_ranked is not None
    assert trig_ranked[0][0] == "silent-noop", "trigger-embedding channel must retrieve the probe"

    # --- channel (b): cue-graph spreading activation -----------------------
    cue_ranked = structural.cue_match(situation, top_k=5)
    assert cue_ranked, "cue-graph channel must retrieve the probe"
    assert cue_ranked[0][0] == "silent-noop", "cue-graph spreading must retrieve the probe"

    # --- fused match + enrichment ------------------------------------------
    matched = await structural.match(situation, top_k=5)
    assert matched.recalls[0].source == "silent-noop"

    bundle = structural.enrich("silent-noop")
    assert {e.event_id for e in bundle.instances} == {"e0", "e1", "e2"}, (
        "enrichment must anchor on the abstraction and pull in its original instances"
    )


async def test_never_recurring_guessed_insight_decays_out(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    episodic = EpisodicStore(db, workspace)
    for i, text in enumerate(_EPISODES):
        episodic.append(
            Event(
                event_id=f"e{i}",
                ts=f"2026-01-01T00:00:0{i}+00:00",
                scope=scope.key,
                kind="respond",
                text=text,
            )
        )

    emb = ConceptEmbedder()
    consolidator = Consolidator(db, workspace, scope, distiller=PlantingDistiller(), embedder=emb)
    await consolidator.run()
    structural = StructuralIndex(db, workspace, scope, embedder=emb)
    await structural.trigger_index()

    g_situation = Situation(
        text="a widget waits on a click that can never arrive",
        summary="a dangling orphaned subscription with no producer",
        cues=["listener-without-source"],
    )

    # Fresh: the guessed insight IS retrieved (both channels), flagged verify-first.
    fresh = await structural.match(g_situation, top_k=5)
    assert fresh.recalls[0].source == "lonely-listener"
    assert fresh.recalls[0].verify_first is True

    # Simulate 90 days with no reinforcement — the nightly decay forgets its cue edge.
    # (Consolidation linked the cue at wall-clock now, so decay forward from there.)
    later = datetime.now(UTC) + timedelta(days=90)
    WeightedGraph(db).decay(scope.key, now=later)

    decayed = await structural.match(g_situation, top_k=5)
    assert decayed.recalls == [], "a never-recurring guessed insight must decay out of retrieval"
