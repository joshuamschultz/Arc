"""Distillation — the ONE LLM path (SDD 4.4, 7; REQ-031/032/033/050).

Consolidation's slow path calls a *bounded structured completion* (one call each,
no agentic loop — OQ-3) to turn a window of raw episodes into additive artifacts:

* **facts** — semantic triplets, applied *additively*: a contradiction folds the
  prior value into a ``was:`` trail (never a destructive overwrite — mem0's
  read-time-resolution lesson, REQ-032), and confidence grows with corroboration
  ``1 - e^(-gamma*hits)`` (REQ-033).
* **insights** — minted abstractions (the centerpiece): a ``trigger`` stated at
  the mechanism level, ``cues`` from the controlled vocabulary (each becomes a
  graph node), and ``instances`` linking the episodes it generalizes. New insights
  start ``guessed`` and only become ``known`` once corroboration crosses the
  confidence threshold (REQ-053).
* **events** — things that happened in the USER's life (a meeting, a sale, a call),
  each recording WHEN it occurred, WHO was in it, and HOW it came out. Participants
  become shared-graph edges, so a person's card is one hop from their history.

The LLM is an **injected seam** (``Distiller`` Protocol), never imported here — so
production wires an arcllm-backed structured completion while tests inject a fake
that returns fixtured payloads. That keeps distillation deterministic-testable and
keeps this module free of any provider dependency.
"""

from __future__ import annotations

import math
from typing import Annotated, Protocol

from pydantic import BaseModel, BeforeValidator, Field

from arcmemory.config import MemoryConfig
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.surface import _cosine
from arcmemory.security import dominating_classification, token_estimate
from arcmemory.slug import canonical_slug
from arcmemory.stores.events import EventStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore, procedure_link_targets
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Confidence, Event, Fact, Insight, LifeEvent, Procedure, Scope


def _coerce_str_items(value: object) -> object:
    """Coerce an LLM-proposed bullet list to ``list[str]`` instead of crashing.

    The distiller occasionally returns items as objects (e.g. ``{"topic": "..."}``)
    rather than plain strings; tolerate that — a dict folds to its joined values, any
    other non-string is stringified — so one odd bullet can't abort the whole day
    summary (and with it the entire consolidation).
    """
    if not isinstance(value, list):
        return value
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append("; ".join(str(v) for v in item.values() if v is not None))
        else:
            out.append(str(item))
    return out


_StrList = Annotated[list[str], BeforeValidator(_coerce_str_items)]


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

    timeline: _StrList = Field(default_factory=list)
    discussions: _StrList = Field(default_factory=list)
    decisions: _StrList = Field(default_factory=list)
    people: _StrList = Field(default_factory=list)
    goals: _StrList = Field(default_factory=list)
    tasks: _StrList = Field(default_factory=list)


class ProcedureCandidate(BaseModel):
    """One reusable how-to the distiller proposes (the structured-output shape).

    ``steps`` is the MERGED method — the distiller is handed the existing card and
    re-states it with this session's refinements folded in. ``dropped_steps`` names the
    stored steps the conversation abandoned (or reworded), verbatim: it is the only way
    a step leaves a card, so a method the session merely referenced is never truncated.
    """

    slug: str
    title: str
    when_to_use: str = ""
    steps: _StrList = Field(default_factory=list)
    dropped_steps: _StrList = Field(default_factory=list)


class ProcedureExtraction(BaseModel):
    """The structured result of the procedure-extraction completion."""

    procedures: list[ProcedureCandidate] = Field(default_factory=list)


class EventCandidate(BaseModel):
    """One thing-that-happened the distiller proposes (the structured-output shape)."""

    slug: str
    title: str
    date: str = ""
    event_type: str = "unknown"
    participants: _StrList = Field(default_factory=list)
    summary: str = ""
    outcome: str = ""


class EventExtraction(BaseModel):
    """The structured result of the life-event-extraction completion."""

    events: list[EventCandidate] = Field(default_factory=list)


class EntityRef(BaseModel):
    """A compact card summary handed to the LLM merge-confirmer (slug + a few facts).

    Just enough for the model to judge identity: the ``slug`` it decides on, the
    human ``name``, the ``entity_type``, and a handful of key ``predicate: value`` facts.
    """

    slug: str
    name: str
    entity_type: str
    facts: list[str] = Field(default_factory=list)


class Distiller(Protocol):
    """The bounded structured-completion seam. Injected, never imported.

    Single-shot calls, no agentic loop: ``extract_facts`` proposes fact triplets;
    ``mint_insights`` proposes abstractions; ``extract_procedures`` proposes reusable
    how-tos MERGED with the existing cards it is handed; ``extract_events`` proposes
    things that happened in the USER's life; ``summarize_day`` condenses a day's events
    into meeting-minutes notes; ``confirm_entity_merges`` conservatively confirms which
    candidate cards are the same real-world entity (the slow-path de-dup gate).
    """

    async def extract_facts(self, events: list[Event]) -> FactExtraction: ...

    async def mint_insights(self, events: list[Event], facts: list[Fact]) -> InsightMint: ...

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction: ...

    async def extract_events(self, episodes: list[Event]) -> EventExtraction: ...

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft: ...

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None: ...

    async def confirm_entity_merges(self, groups: list[list[EntityRef]]) -> list[list[str]]: ...


class EntityMergeConfirmer(Protocol):
    """The single-method seam that gates slow-path entity de-duplication.

    Given CANDIDATE clusters (same-type cards whose names merely embed close), it
    returns the slug sub-groups that are UNAMBIGUOUSLY the same real-world entity —
    each returned sub-group has >= 2 slugs, and different-but-similar entities (a
    'Josh Schultz' vs a 'Joshua Shubbie') are kept apart. Any :class:`Distiller`
    satisfies it structurally, so production passes the same arcllm-backed distiller;
    tests inject a stub with only this method. Merge is never done on embedding alone.
    """

    async def confirm_entity_merges(self, groups: list[list[EntityRef]]) -> list[list[str]]: ...


class EntityDisambiguator(Protocol):
    """The single-method seam used by search-before-write identity resolution.

    Asks whether a new entity candidate is the SAME real-world thing as one of a few
    existing cards (the ``candidates`` canonical slugs), returning the matching slug or
    ``None``. Any :class:`Distiller` satisfies it structurally, so production passes the
    same arcllm-backed distiller; tests inject a stub with only this method.
    """

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None: ...


async def resolve_entity(
    store: SemanticStore,
    *,
    slug: str,
    name: str,
    entity_type: str,
    embedder: Embedder | None = None,
    distiller: EntityDisambiguator | None = None,
    config: MemoryConfig | None = None,
) -> str:
    """Resolve a distiller-proposed entity onto its canonical slug (search-before-write).

    In order: (1) an exact canonical-slug file / (2) a recorded alias resolves to the
    existing card (both deterministic, embedder-free — this alone collapses common
    variants and closes the re-dup loop); (3) with an embedder, a same-type name whose
    cosine clears ``entity_merge_threshold`` folds onto the nearest card; (4) with a
    distiller AND an ambiguous near match, one bounded LLM call decides; (5) otherwise
    it is genuinely new and the canonical slug is returned. Never raises when the
    embedder/distiller is absent — steps 1-2 always run.
    """
    cfg = config or MemoryConfig()
    deterministic = store.resolve(slug, name)
    if store.read(deterministic) is not None:
        return deterministic  # exact-file or alias hit -> an existing card
    if embedder is None:
        return deterministic  # brand-new; nothing to embed against
    match, near = await _fuzzy_entity_match(store, name, entity_type, embedder, cfg)
    if match is not None:
        return match
    if distiller is not None and near:
        chosen = await distiller.disambiguate_entity(name, entity_type, near)
        if chosen and store.read(canonical_slug(chosen)) is not None:
            return canonical_slug(chosen)
    return deterministic


async def _fuzzy_entity_match(
    store: SemanticStore,
    name: str,
    entity_type: str,
    embedder: Embedder,
    config: MemoryConfig,
) -> tuple[str | None, list[str]]:
    """Embedding fuzz over SAME-TYPE cards, plus an exact-name cross-type namesake.

    Returns ``(match, near)`` — ``match`` is the canonical slug at/above the merge
    threshold (fold now; same-type only — an embedding score alone is never enough
    to auto-fold across types). ``near`` is the disambiguation band, worth an LLM
    call rather than an auto-fold: same-type slugs scoring in
    ``[entity_disambiguate_min, entity_merge_threshold)``, PLUS any slug of a
    DIFFERENT type whose name matches ``name`` exactly (case-insensitive) — the
    fix for an entity whose type itself drifted between writes (filed once as
    "thing", once as "skill" for the same real card), which same-type-only
    scoring can never see since it never compares across types. The exact-name
    signal needs no embedder, so it is still returned when embeddings are
    unavailable; ``match`` stays ``None`` in that case so the caller degrades
    cleanly (no LLM to ask, no auto-fold to fall back to).
    """
    cross_type_exact = [
        s
        for s in store.slugs()
        if (e := store.read(s))
        and e.entity_type != entity_type
        and e.name.strip().lower() == name.strip().lower()
    ]
    same_type = [
        (s, e) for s in store.slugs() if (e := store.read(s)) and e.entity_type == entity_type
    ]
    if not same_type:
        return None, cross_type_exact
    embedded = await embed_or_none(embedder, [name] + [e.name for _, e in same_type])
    if embedded is None:
        return None, cross_type_exact
    query, existing = embedded[0], embedded[1:]
    scored = sorted(
        ((slug, _cosine(query, vec)) for (slug, _e), vec in zip(same_type, existing, strict=True)),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best_slug, best_score = scored[0]
    if best_score >= config.entity_merge_threshold:
        return best_slug, []
    near = [slug for slug, score in scored if score >= config.entity_disambiguate_min]
    return None, near + cross_type_exact


def confidence_from_hits(hits: float, gamma: float) -> float:
    """Memory confidence ``1 - e^(-gamma*hits)`` — rises, saturating, with corroboration."""
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
    embedder: Embedder | None = None,
) -> list[tuple[str, Fact]]:
    """Apply the distiller's facts additively; return the (slug, fact) mutations.

    Each candidate slug is first resolved onto its canonical card (search-before-write,
    :func:`resolve_entity`) so a variant spelling folds into the existing entity instead
    of minting a duplicate. Over-budget windows are distilled in sequential chunks and
    their facts assembled here. Corroboration accumulates: when the value is unchanged
    the prior confidence is inverted back to hits and the window's hits are added, so a
    repeated fact grows more confident. A changed value is a contradiction —
    ``write_fact`` folds the prior into a ``was:`` trail rather than erasing it.
    """
    applied: list[tuple[str, Fact]] = []
    for chunk in chunk_events(events, config.distill_max_input_tokens):
        extraction = await distiller.extract_facts(chunk)
        for cand in extraction.facts:
            resolved = await resolve_entity(
                store,
                slug=cand.slug,
                name=cand.name or cand.slug,
                entity_type=cand.entity_type,
                embedder=embedder,
                distiller=distiller,
                config=config,
            )
            confidence = _accumulated_confidence(store, resolved, cand, config.gamma)
            entity = store.write_fact(
                resolved,
                cand.predicate,
                cand.value,
                confidence=confidence,
                name=cand.name,
                entity_type=cand.entity_type,
                classification=cand.classification,
            )
            fact = next(f for f in entity.facts if f.predicate == cand.predicate)
            applied.append((resolved, fact))
    return applied


def _accumulated_confidence(
    store: SemanticStore, slug: str, cand: FactCandidate, gamma: float
) -> float:
    """Confidence for a candidate, accumulating prior hits when the value is unchanged."""
    entity = store.read(slug)
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
    graph: WeightedGraph,
    scope: Scope,
) -> list[Procedure]:
    """Evolve the how-to cards from this window; wire each into the graph; return them.

    The distiller is handed the CURRENT cards (title, trigger, steps) so it returns the
    merged method — a step can be added, reworded, reordered, or explicitly dropped. The
    store then folds that answer in non-lossily, so a step the window merely failed to
    mention survives. Cards are re-read per chunk, so an over-budget window's later
    chunks build on what its earlier chunks already wrote. Every ``[[slug]]`` the card
    names becomes a graph edge, which is what makes the method resurface on retrieval.
    Candidates without a slug or steps are skipped (nothing findable to store).
    """
    upserted: list[Procedure] = []
    for chunk in chunk_events(events, config.distill_max_input_tokens):
        result = await distiller.extract_procedures(chunk, _existing_procedures(store))
        for cand in result.procedures:
            if not cand.slug or not cand.steps:
                continue
            procedure = store.upsert(
                cand.slug,
                cand.title,
                when_to_use=cand.when_to_use,
                steps=cand.steps,
                dropped=cand.dropped_steps,
            )
            for target in procedure_link_targets(procedure):
                graph.link(scope.key, procedure.slug, target, kind="link")
            upserted.append(procedure)
    return upserted


def _existing_procedures(store: ProceduralStore) -> list[Procedure]:
    """Every stored how-to card — what the distiller merges its answer into."""
    return [card for slug in store.slugs() if (card := store.read(slug)) is not None]


async def extract_events(
    episodes: list[Event],
    *,
    distiller: Distiller,
    store: EventStore,
    graph: WeightedGraph,
    scope: Scope,
    config: MemoryConfig,
) -> list[LifeEvent]:
    """Record what happened in the USER's life; wire each participant as a graph edge.

    An occurrence happens once, so a re-extraction refreshes the card in place rather
    than accumulating hits. Undated candidates fall back to the day they were discussed.
    Each card inherits the dominating classification of the episodes it was learned from,
    so an event can never launder a classified conversation down to a lower clearance.
    Candidates without a slug or title are skipped (nothing findable to store).
    """
    recorded: list[LifeEvent] = []
    for chunk in chunk_events(episodes, config.distill_max_input_tokens):
        result = await distiller.extract_events(chunk)
        for cand in result.events:
            if not cand.slug or not cand.title:
                continue
            event = store.upsert(
                cand.slug,
                cand.title,
                date=cand.date or chunk[0].ts[:10],
                event_type=cand.event_type,
                participants=cand.participants,
                summary=cand.summary,
                outcome=cand.outcome,
                classification=dominating_classification([e.classification for e in chunk]),
            )
            for participant in event.participants:
                graph.link(scope.key, event.slug, participant, kind="link")
            recorded.append(event)
    return recorded


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
    "EntityDisambiguator",
    "EntityMergeConfirmer",
    "EntityRef",
    "EventCandidate",
    "EventExtraction",
    "FactCandidate",
    "FactExtraction",
    "InsightCandidate",
    "InsightMint",
    "ProcedureCandidate",
    "ProcedureExtraction",
    "chunk_events",
    "confidence_from_hits",
    "extract_events",
    "extract_facts",
    "extract_procedures",
    "hits_from_confidence",
    "mint_insights",
    "resolve_entity",
]
