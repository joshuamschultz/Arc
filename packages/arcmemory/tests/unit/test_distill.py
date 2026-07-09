"""T-050/051 — distillation: additive facts (`was:` trail), minted insights.

Distillation is the ONE LLM path, and it is injected as a seam so tests are
deterministic: a ``FakeDistiller`` returns fixtured facts/insights instead of
hitting a provider. What the tests prove is the arcmemory *logic* around that
call — additive `was:` trails, confidence that rises with corroboration, and an
insight whose trigger is genuinely surface-stripped (shares no token with the
episodes it generalizes).
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    FactCandidate,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    confidence_from_hits,
    extract_facts,
    mint_insights,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, Scope


class FakeDistiller:
    """Injected structured-completion stub — returns fixtured facts/insights."""

    def __init__(self, extraction: FactExtraction, mint: InsightMint | None = None) -> None:
        self._extraction = extraction
        self._mint = mint or InsightMint()
        self.extract_calls = 0
        self.mint_calls = 0

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        self.extract_calls += 1
        return self._extraction

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        self.mint_calls += 1
        return self._mint


def _semantic(db: MemoryDB, workspace: Path, scope: Scope) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope=scope.key)


# -- T-050: additive facts, `was:` trail, confidence rises ------------------


async def test_contradiction_writes_was_trail_not_overwrite(workspace, db, scope) -> None:
    store = _semantic(db, workspace, scope)
    store.write_fact("alice", "role", "engineer", confidence=0.6)

    distiller = FakeDistiller(
        FactExtraction(facts=[FactCandidate(slug="alice", predicate="role", value="manager")])
    )
    await extract_facts([], distiller=distiller, store=store, config=MemoryConfig())

    entity = store.read("alice")
    assert entity is not None
    fact = next(f for f in entity.facts if f.predicate == "role")
    assert fact.value == "manager"  # new value applied
    assert fact.was_value == "engineer"  # prior folded into the trail, not erased


async def test_confidence_rises_with_repeat_mentions(workspace, db, scope) -> None:
    store = _semantic(db, workspace, scope)
    cfg = MemoryConfig()

    distiller = FakeDistiller(
        FactExtraction(facts=[FactCandidate(slug="bob", predicate="team", value="arc", hits=1)])
    )
    await extract_facts([], distiller=distiller, store=store, config=cfg)
    first = store.read("bob").facts[0].confidence

    # Same fact mentioned again next window -> corroboration accumulates.
    await extract_facts([], distiller=distiller, store=store, config=cfg)
    second = store.read("bob").facts[0].confidence

    assert second > first
    # Stored confidence is rounded to 2 dp by the markdown triplet format.
    assert abs(first - confidence_from_hits(1, cfg.gamma)) < 0.01


# -- T-051: minted insight with a surface-stripped trigger ------------------


async def test_mint_insight_from_lexically_different_episodes(workspace, db, scope) -> None:
    """A cluster of structurally-similar, lexically-DIFFERENT episodes -> one insight.

    The episodes talk about budgets, a trifecta gate, and signing — sharing no
    salient token. The minted trigger is stated at the *mechanism* level and must
    share no surface token with any episode (proving genuine abstraction).
    """
    episodes = [
        Event(
            event_id="e0", scope=scope.key, kind="obs", text="the budget breaker was unreachable"
        ),
        Event(
            event_id="e1", scope=scope.key, kind="obs", text="trifecta gate had no leg producers"
        ),
        Event(
            event_id="e2",
            scope=scope.key,
            kind="obs",
            text="signing predicate existed but nothing called it",
        ),
    ]
    insight = InsightCandidate(
        id="producers-unwired",
        statement="A guard exists but is never invoked.",
        trigger="a guarantee is asserted yet the mechanism enforcing it is never connected to the flow",
        cues=["claims-property", "predicate-without-producer"],
        instances=["e0", "e1", "e2"],
    )

    store = InsightStore(workspace)
    graph = WeightedGraph(db)
    distiller = FakeDistiller(FactExtraction(), InsightMint(insights=[insight]))

    minted = await mint_insights(
        episodes,
        [],
        distiller=distiller,
        store=store,
        graph=graph,
        scope=scope,
        config=MemoryConfig(),
    )

    assert len(minted) == 1
    card = store.read("producers-unwired")
    assert card is not None
    assert card.status.value == "guessed"  # guessed on first mint
    assert len(card.cues) >= 1
    assert set(card.instances) == {"e0", "e1", "e2"}  # instance links to episodes

    # The trigger is surface-stripped: it shares NO salient token with any episode.
    trigger_tokens = set(card.trigger.lower().split())
    for ep in episodes:
        salient = {t for t in ep.text.lower().split() if len(t) > 3}
        assert not (trigger_tokens & salient), f"trigger leaked a surface token from {ep.text!r}"

    # Each cue became a graph node (an insight->cue edge exists).
    neighbors = {node for node, _ in graph.neighbors(scope.key, "producers-unwired")}
    assert "claims-property" in neighbors
    assert "predicate-without-producer" in neighbors
