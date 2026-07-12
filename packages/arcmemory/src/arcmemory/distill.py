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
from arcmemory.security import dominating_classification, token_estimate
from arcmemory.slug import canonical_slug
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Confidence, Event, Fact, Insight, Procedure, Scope


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
    """The distiller's proposed daily notes — meeting-minutes shape.

    Bulleted lists only. ``timeline`` is time-stamped + chronological; the rest give
    the topic/decision/goal/task detail. ``day`` and ``classification`` are derived by
    the caller (not the LLM), so they are absent here.
    """

    timeline: list[str] = Field(default_factory=list)
    discussions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)


class ProcedureCandidate(BaseModel):
    """One reusable how-to the distiller proposes (the structured-output shape)."""

    slug: str
    title: str
    when_to_use: str = ""
    steps: list[str] = Field(default_factory=list)


class ProcedureExtraction(BaseModel):
    """The structured result of the procedure-extraction completion."""

    procedures: list[ProcedureCandidate] = Field(default_factory=list)


class Distiller(Protocol):
    """The bounded structured-completion seam. Injected, never imported.

    Single-shot calls, no agentic loop: ``extract_facts`` proposes fact triplets;
    ``mint_insights`` proposes abstractions; ``extract_procedures`` proposes reusable
    how-tos; ``summarize_day`` condenses a day's events into meeting-minutes notes.
    """

    async def extract_facts(self, events: list[Event]) -> FactExtraction: ...

    async def mint_insights(self, events: list[Event], facts: list[Fact]) -> InsightMint: ...

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction: ...

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft: ...


def confidence_from_hits(hits: float, gamma: float) -> float:
    """FERNme confidence ``1 - e^(-gamma*hits)`` — rises, saturating, with corroboration."""
    return 1.0 - math.exp(-gamma * max(0.0, hits))


def hits_from_confidence(confidence: float, gamma: float) -> float:
    """Invert ``confidence_from_hits`` to recover accumulated hits (for additive growth)."""
    clamped = min(max(confidence, 0.0), 0.999999)
    return -math.log(1.0 - clamped) / gamma


def chunk_events(events: list[Event], max_tokens: int | None) -> list[list[Event]]:
    """Split a window into consecutive chunks each within the token budget.

    The distiller (arcllm-backed in production) has a finite context, so a large
    window is fed as several *sequential* calls instead of one 165k-token call
    that overflows. ``max_tokens=None`` disables chunking (one chunk). A single
    event larger than the budget cannot be split without corrupting the record, so
    it ships alone — if that still overflows, the provider seam surfaces it (see
    the module TODO / follow-up note).
    """
    if max_tokens is None or len(events) <= 1:
        return [events] if events else []
    chunks: list[list[Event]] = []
    current: list[Event] = []
    running = 0
    for event in events:
        cost = token_estimate(event.text)
        if current and running + cost > max_tokens:
            chunks.append(current)
            current, running = [], 0
        current.append(event)
        running += cost
    if current:
        chunks.append(current)
    return chunks


async def extract_facts(
    events: list[Event],
    *,
    distiller: Distiller,
    store: SemanticStore,
    config: MemoryConfig,
) -> list[tuple[str, Fact]]:
    """Apply the distiller's facts additively; return the (slug, fact) mutations.

    Over-budget windows are distilled in sequential chunks and their facts
    assembled here. Corroboration accumulates: when the value is unchanged the
    prior confidence is inverted back to hits and the window's hits are added, so a
    repeated fact grows more confident. A changed value is a contradiction —
    ``write_fact`` folds the prior into a ``was:`` trail rather than erasing it.
    """
    applied: list[tuple[str, Fact]] = []
    for chunk in chunk_events(events, config.distill_max_input_tokens):
        extraction = await distiller.extract_facts(chunk)
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


async def extract_procedures(
    events: list[Event],
    *,
    distiller: Distiller,
    store: ProceduralStore,
    config: MemoryConfig,
) -> list[Procedure]:
    """Extract reusable how-tos from the window; upsert each as a procedure card.

    Over-budget windows are distilled in sequential chunks. A re-extracted
    procedure bumps its ``use_count`` (reinforcement), so a process that recurs
    across windows (or chunks) becomes more prominent. Candidates without a slug or
    steps are skipped (nothing findable to store).
    """
    upserted: list[Procedure] = []
    for chunk in chunk_events(events, config.distill_max_input_tokens):
        result = await distiller.extract_procedures(chunk)
        for cand in result.procedures:
            if not cand.slug or not cand.steps:
                continue
            upserted.append(
                store.upsert(cand.slug, cand.title, when_to_use=cand.when_to_use, steps=cand.steps)
            )
    return upserted


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
    by_id = {e.event_id: e for e in events}
    minted: list[Insight] = []
    for chunk in chunk_events(events, config.distill_max_input_tokens):
        result = await distiller.mint_insights(chunk, facts)
        for cand in result.insights:
            cand.id = canonical_slug(cand.id)
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
    "DaySummaryDraft",
    "Distiller",
    "FactCandidate",
    "FactExtraction",
    "InsightCandidate",
    "InsightMint",
    "ProcedureCandidate",
    "ProcedureExtraction",
    "chunk_events",
    "confidence_from_hits",
    "extract_facts",
    "extract_procedures",
    "hits_from_confidence",
    "mint_insights",
]
