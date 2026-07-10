"""Distillation — the ONE LLM path (SDD 4.4, 7; REQ-031/032/033/050).

Consolidation's slow path calls a *bounded structured completion* (one call each,
no agentic loop — OQ-3) to turn a window of raw episodes into two additive
artifacts:

* **facts** — semantic triplets, applied *additively*: a contradiction folds the
  prior value into a ``was:`` trail (never a destructive overwrite — mem0's
  read-time-resolution lesson, REQ-032), and confidence grows with corroboration
  ``1 - e^(-gamma*hits)`` (REQ-033).
* **insights** — minted abstractions (the centerpiece): a ``trigger`` stated at
  the mechanism level, ``cues`` from the controlled vocabulary (each becomes a
  graph node), and ``instances`` linking the episodes it generalizes. New insights
  start ``guessed`` and only become ``known`` once corroboration crosses the
  confidence threshold (REQ-053).

The LLM is an **injected seam** (``Distiller`` Protocol), never imported here — so
production wires an arcllm-backed structured completion while tests inject a fake
that returns fixtured payloads. That keeps distillation deterministic-testable and
keeps this module free of any provider dependency.
"""

from __future__ import annotations

import math
from typing import Protocol

from pydantic import BaseModel, Field

from arcmemory.config import MemoryConfig
from arcmemory.index.graph import WeightedGraph
from arcmemory.security import dominating_classification
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Confidence, Event, Fact, Insight, Scope


class FactCandidate(BaseModel):
    """One fact the distiller proposes for a window (the structured-output shape)."""

    slug: str
    predicate: str
    value: str
    hits: int = 1
    name: str | None = None
    entity_type: str = "unknown"
    classification: str = "unclassified"


class FactExtraction(BaseModel):
    """The structured result of the fact-extraction completion."""

    facts: list[FactCandidate] = Field(default_factory=list)


class InsightCandidate(BaseModel):
    """One insight the distiller proposes (the minted-abstraction shape)."""

    id: str
    statement: str
    trigger: str
    cues: list[str] = Field(default_factory=list)
    instances: list[str] = Field(default_factory=list)
    hits: int = 1


class InsightMint(BaseModel):
    """The structured result of the insight-minting completion."""

    insights: list[InsightCandidate] = Field(default_factory=list)


class DaySummaryDraft(BaseModel):
    """The distiller's proposed daily rollup (the structured-output shape).

    Bulleted lists only — the curated, high-signal condensation of a day's raw
    events. ``day`` and ``classification`` are derived by the caller (not the LLM),
    so they are absent here.
    """

    summary: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)


class Distiller(Protocol):
    """The bounded structured-completion seam. Injected, never imported.

    Three single-shot calls, no agentic loop: ``extract_facts`` reads the window and
    proposes fact triplets; ``mint_insights`` reads the window + the freshly extracted
    facts and proposes abstractions; ``summarize_day`` condenses a day's events into
    the curated daily-notes bullets.
    """

    async def extract_facts(self, events: list[Event]) -> FactExtraction: ...

    async def mint_insights(self, events: list[Event], facts: list[Fact]) -> InsightMint: ...

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft: ...


def confidence_from_hits(hits: float, gamma: float) -> float:
    """FERNme confidence ``1 - e^(-gamma*hits)`` — rises, saturating, with corroboration."""
    return 1.0 - math.exp(-gamma * max(0.0, hits))


def hits_from_confidence(confidence: float, gamma: float) -> float:
    """Invert ``confidence_from_hits`` to recover accumulated hits (for additive growth)."""
    clamped = min(max(confidence, 0.0), 0.999999)
    return -math.log(1.0 - clamped) / gamma


async def extract_facts(
    events: list[Event],
    *,
    distiller: Distiller,
    store: SemanticStore,
    config: MemoryConfig,
) -> list[tuple[str, Fact]]:
    """Apply the distiller's facts additively; return the (slug, fact) mutations.

    Corroboration accumulates: when the value is unchanged the prior confidence is
    inverted back to hits and the window's hits are added, so a repeated fact grows
    more confident. A changed value is a contradiction — ``write_fact`` folds the
    prior into a ``was:`` trail rather than erasing it.
    """
    extraction = await distiller.extract_facts(events)
    applied: list[tuple[str, Fact]] = []
    for cand in extraction.facts:
        confidence = _accumulated_confidence(store, cand, config.gamma)
        entity = store.write_fact(
            cand.slug,
            cand.predicate,
            cand.value,
            confidence=confidence,
            name=cand.name,
            entity_type=cand.entity_type,
            classification=cand.classification,
        )
        fact = next(f for f in entity.facts if f.predicate == cand.predicate)
        applied.append((cand.slug, fact))
    return applied


def _accumulated_confidence(store: SemanticStore, cand: FactCandidate, gamma: float) -> float:
    """Confidence for a candidate, accumulating prior hits when the value is unchanged."""
    entity = store.read(cand.slug)
    prior = None
    if entity is not None:
        prior = next((f for f in entity.facts if f.predicate == cand.predicate), None)
    prior_hits = (
        hits_from_confidence(prior.confidence, gamma)
        if prior is not None and prior.value == cand.value
        else 0.0
    )
    return confidence_from_hits(prior_hits + cand.hits, gamma)


async def mint_insights(
    events: list[Event],
    facts: list[Fact],
    *,
    distiller: Distiller,
    store: InsightStore,
    graph: WeightedGraph,
    scope: Scope,
    config: MemoryConfig,
) -> list[Insight]:
    """Mint/corroborate insights; wire each cue as a graph node; return the cards.

    A first mint starts ``guessed``; a re-mint accumulates hits (and merges cues +
    instances), promoting to ``known`` once confidence crosses the threshold.
    """
    result = await distiller.mint_insights(events, facts)
    by_id = {e.event_id: e for e in events}
    minted: list[Insight] = []
    for cand in result.insights:
        insight = _apply_insight(store.read(cand.id), cand, config, by_id)
        store.write(insight)
        for cue in insight.cues:
            graph.link(scope.key, insight.id, cue, kind="cue")
        minted.append(insight)
    return minted


def _apply_insight(
    existing: Insight | None,
    cand: InsightCandidate,
    config: MemoryConfig,
    events_by_id: dict[str, Event],
) -> Insight:
    """Fold a candidate into an existing card (or mint fresh); set status by confidence.

    The card inherits the MAX classification of the episodes it generalizes (plus any
    prior card's label), so an abstraction can never launder a classified episode down
    to a lower clearance — and an unknown-labeled instance keeps the card fail-closed.
    """
    hits = (existing.hits if existing else 0) + cand.hits
    cues = _merge_unique(existing.cues if existing else [], cand.cues)
    instances = _merge_unique(existing.instances if existing else [], cand.instances)
    confidence = confidence_from_hits(hits, config.gamma)
    status = Confidence.KNOWN if confidence >= config.known_threshold else Confidence.GUESSED
    labels = [events_by_id[i].classification for i in instances if i in events_by_id]
    if existing is not None:
        labels.append(existing.classification)
    return Insight(
        id=cand.id,
        statement=cand.statement,
        trigger=cand.trigger,
        cues=cues,
        instances=instances,
        classification=dominating_classification(labels),
        confidence=confidence,
        salience=existing.salience if existing else 0.0,
        status=status,
        hits=hits,
    )


def _merge_unique(existing: list[str], new: list[str]) -> list[str]:
    """Union two lists preserving first-seen order (stable, dedup'd)."""
    merged = list(existing)
    for item in new:
        if item not in merged:
            merged.append(item)
    return merged


__all__ = [
    "Distiller",
    "FactCandidate",
    "FactExtraction",
    "InsightCandidate",
    "InsightMint",
    "confidence_from_hits",
    "extract_facts",
    "hits_from_confidence",
    "mint_insights",
]
