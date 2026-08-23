"""Typed data models for arcmemory (Pydantic v2).

Every value that crosses a store, index, or retrieval boundary is one of these
models. They are the contract the ``Brain`` Protocol speaks; keeping them here
(and free of any I/O) is what lets the stores stay swappable and the whole
package type-check under ``mypy --strict``.

Two vocabularies worth stating once:

* **Scope** — the shared-nothing isolation key. Every capture, every recall, every
  edge belongs to exactly one ``Scope`` (an agent DID, optionally narrowed to a
  session). No cross-scope table ever holds another scope's plaintext (LLM08).
* **confidence / salience** — the two scalars that drive memory dynamics.
  ``confidence`` grows with corroboration (``1 - e^(-gamma*hits)``) and separates a
  ``guessed`` memory (verify first) from a ``known`` one (actionable). ``salience``
  slows forgetting so a rare-but-significant signal survives decay.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Confidence growth rate, shared with insight corroboration so the "known" bar
#: (0.8, reached at 3 hits) means one thing across every kind of memory.
_STEP_GAMMA = 0.536


def confidence_from_hits(hits: float, gamma: float) -> float:
    """Memory confidence ``1 - e^(-gamma*hits)`` — rises, saturating, with corroboration."""
    return 1.0 - math.exp(-gamma * max(0.0, hits))


def hits_from_confidence(confidence: float, gamma: float) -> float:
    """Invert :func:`confidence_from_hits` to recover accumulated hits (additive growth)."""
    clamped = min(max(confidence, 0.0), 0.999999)
    return -math.log(1.0 - clamped) / gamma


def _utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string (the canonical ts format)."""
    return datetime.now(UTC).isoformat()


def utc_today() -> str:
    """Today's UTC calendar date (``YYYY-MM-DD``) — the canonical card-date format."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


class Confidence(StrEnum):
    """Whether a memory may be acted on directly or must be verified first.

    ``guessed`` - seen once / low corroboration; surfaced tentatively.
    ``known`` - recurred and corroborated; an actionable anchor.
    """

    GUESSED = "guessed"
    KNOWN = "known"


class Scope(BaseModel):
    """Per-agent, shared-nothing isolation key.

    ``agent_did`` is mandatory (there is no memory without an identity). A
    ``session_id`` optionally narrows the scope further. ``key`` is the stable
    string used to isolate on-disk state and every derived-index row.
    """

    model_config = ConfigDict(frozen=True)

    agent_did: str
    session_id: str | None = None

    @property
    def key(self) -> str:
        """Stable isolation key: ``<did>`` or ``<did>:<session>``."""
        return self.agent_did if self.session_id is None else f"{self.agent_did}:{self.session_id}"


class Event(BaseModel):
    """One raw episodic event — the high-volume append-only stream row.

    ``kind`` is the source shape (e.g. ``tool``, ``respond``, ``observation``).
    ``hash`` is the windowed-dedup content hash. ``refs`` links related events or
    entities (adjacency for enrichment).
    """

    event_id: str
    ts: str = Field(default_factory=_utc_now_iso)
    scope: str
    kind: str
    text: str
    hash: str = ""
    classification: str = "unclassified"
    salience: float = 0.0
    refs: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    #: ISO-8601 from the source system (empty = unknown); drives last-writer-wins
    #: ordering in :func:`arcmemory.ingest.ingest_batch` (SPEC-073).
    source_updated_at: str = ""


class Fact(BaseModel):
    """A compact semantic fact-triplet about an entity.

    Rendered to markdown as ``predicate: value .confidence date`` with an optional
    ``| was: prior .conf`` contradiction trail (additive, never destructive).
    """

    predicate: str
    value: str
    confidence: float = 0.5
    date: str = Field(default_factory=utc_today)
    was_value: str | None = None
    was_confidence: float | None = None


class Entity(BaseModel):
    """A person/place/project — a node in the semantic graph.

    ``facts`` are its triplets; ``links_to`` are wiki-link edges to other entities.
    ``classification`` + ``cross_session_visibility`` drive the no-read-up gate.
    """

    slug: str
    name: str
    entity_type: str = "unknown"
    classification: str = "unclassified"
    cross_session_visibility: bool = False
    confidence: float = 0.5
    facts: list[Fact] = Field(default_factory=list)
    links_to: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    """Names/slugs of duplicate cards folded into this one (entity-merge trail)."""


class DaySummary(BaseModel):
    """A day's curated notes — meeting-minutes for the raw transcript.

    Rich enough to reconstruct WHAT happened, WHY, and WHEN: a chronological
    ``timeline`` (each bullet time-stamped), topic ``discussions`` (what + method +
    why), ``decisions`` (with rationale), ``people`` (who/where + what about them),
    ``goals`` (targets), and ``tasks`` (action items). Bullets may carry ``[[slug]]``
    wiki-links to entity/procedure/insight cards so an agent can hop between memories.

    The raw transcript stays in the episodic stream + audit log, never here (glass-box,
    high-signal). ``classification`` is the dominating label of the day's events, so the
    file channel is gated exactly like the raw stream.
    """

    day: str  # YYYY-MM-DD
    timeline: list[str] = Field(default_factory=list)
    discussions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    classification: str = "unclassified"

    def is_empty(self) -> bool:
        """True when no section carries a bullet (nothing worth a file)."""
        return not (
            self.timeline
            or self.discussions
            or self.decisions
            or self.people
            or self.goals
            or self.tasks
        )


class Step(BaseModel):
    """One step of a procedure, with how often it has been corroborated.

    A procedure is not uniformly trustworthy: some steps are the operator's firm
    practice, restated whenever the topic comes up, and others were said once and
    may have been a one-off. As bare strings the two are indistinguishable, so a
    card cannot tell its reader which parts are the method.

    ``hits`` counts the sessions that stated this step; ``confidence`` is the same
    ``1 - e^(-gamma*hits)`` curve insights use, so "three mentions and it is known"
    means the same thing everywhere in memory.
    """

    text: str
    hits: int = 1

    @property
    def confidence(self) -> float:
        """Corroboration as a 0-1 scalar (gamma 0.536: 1 hit .41, 3 hits .80)."""
        return confidence_from_hits(self.hits, _STEP_GAMMA)

    def __str__(self) -> str:
        """The step's text — every surface that prints a step prints the words."""
        return self.text


class ProcedureSummary(NamedTuple):
    """One line of the procedure index: enough to choose a card, without its steps.

    A fleet accumulates dozens of procedures and their full step lists will not fit
    in a turn. The trigger (``when_to_use``) is what decides relevance, so it is the
    one body field worth carrying — and keeping steps off this type means a caller
    cannot accidentally spend the context it exists to save.
    """

    slug: str
    title: str
    when_to_use: str
    use_count: int
    revisions: int


class Procedure(BaseModel):
    """A how-to card — a repeatable process, findable by its trigger.

    ``when_to_use`` is the situation to match against later (kept searchable so the
    right procedure surfaces when a similar task recurs). ``steps`` may carry
    ``[[slug]]`` links to the entities/tools they involve.
    """

    slug: str
    title: str
    when_to_use: str = ""
    steps: list[Step] = Field(default_factory=list)
    #: Times this playbook was actually REACHED FOR — bumped by ``ProceduralStore.use``.
    #: Distinct from ``revisions`` because the two answer different questions, and
    #: conflating them made a much-refined procedure indistinguishable from a
    #: much-used one: a live store of 36 read 28 at 1 and 8 at 2, none of which was
    #: evidence of use, because only writes were ever counted.
    use_count: int = 0
    #: Times the card was written or evolved — how settled the playbook is.
    revisions: int = 0
    classification: str = "unclassified"

    @property
    def step_texts(self) -> list[str]:
        """Just the wording, for callers that do not care how settled each step is."""
        return [step.text for step in self.steps]

    @field_validator("steps", mode="before")
    @classmethod
    def _accept_plain_steps(cls, value: object) -> object:
        """Accept bare strings as steps — a string means "stated once".

        Callers that only have step text (the distiller, hygiene, any caller who
        does not care about corroboration) stay simple, and there is exactly one
        reading of a step with no counter attached.
        """
        if isinstance(value, list):
            return [Step(text=item, hits=1) if isinstance(item, str) else item for item in value]
        return value


class LifeEvent(BaseModel):
    """A thing that HAPPENED in the user's life — a meeting, a sale, a call, a shipment.

    Not to be confused with :class:`Event`, the raw episodic *stream row*. This is the
    semantic card: WHAT occurred, WHEN it occurred (``date`` — deliberately distinct
    from ``recorded``, the day memory wrote it down), WHO was in it (``participants``,
    entity slugs rendered as ``[[wiki-links]]`` and wired into the shared graph), and
    HOW it came out (``outcome``). It is the *user's* timeline; the agent's own day is
    the daily notes (:class:`DaySummary`), and how the agent improves is policy.
    """

    slug: str
    title: str
    date: str = ""
    """``YYYY-MM-DD`` the event happened (empty when the conversation never said)."""
    recorded: str = Field(default_factory=utc_today)
    """``YYYY-MM-DD`` memory first wrote the card — never the same field as ``date``."""
    event_type: str = "unknown"
    """meeting | sale | call | shipment | ... (open vocabulary, the user's domain)."""
    participants: list[str] = Field(default_factory=list)
    summary: str = ""
    outcome: str = ""
    classification: str = "unclassified"


class Insight(BaseModel):
    """A minted abstraction — the centerpiece store.

    ``trigger`` is the situation stated at the *mechanism* level (surface stripped),
    embedded into abstraction space. ``cues`` are the abstract feature tags.
    ``instances`` link to the episodes it generalizes (enrichment targets).
    """

    id: str
    statement: str
    trigger: str
    cues: list[str] = Field(default_factory=list)
    instances: list[str] = Field(default_factory=list)
    classification: str = "unclassified"
    confidence: float = 0.0
    salience: float = 0.0
    status: Confidence = Confidence.GUESSED
    hits: int = 0


class Situation(BaseModel):
    """The current turn abstracted for structural retrieval.

    ``text`` is the raw turn/goal text; ``summary`` is the reused turn summary
    (default abstraction — no new LLM call); ``cues`` are the cue nodes lit by the
    current situation.
    """

    text: str
    summary: str = ""
    cues: list[str] = Field(default_factory=list)


class TimeWindow(BaseModel):
    """The slice of the raw stream one consolidation run reads.

    ``start``/``end`` are inclusive ISO-8601 bounds; either may be ``None`` to
    mean "unbounded on that side" (a ``None``/``None`` window is the whole stream).
    Capping the window is what makes each consolidation's cost bounded (LLM10).
    """

    start: str | None = None
    end: str | None = None

    def contains(self, ts: str) -> bool:
        """Whether ``ts`` falls within the (inclusive) window bounds."""
        if self.start is not None and ts < self.start:
            return False
        return not (self.end is not None and ts > self.end)


class Recall(BaseModel):
    """One retrieved item, ready to be boundary-marked and injected.

    ``score`` is the fused rank score; ``confidence`` gates action; ``classification``
    is the label the no-read-up gate checked.
    """

    source: str
    content: str
    score: float
    kind: str = "surface"
    confidence: Confidence = Confidence.KNOWN
    classification: str = "unclassified"
    verify_first: bool = False
    #: WHEN the underlying memory was established (``YYYY-MM-DD``), so the model can
    #: judge staleness. Empty string when no usable timestamp is known (unstamped).
    established: str = ""


class RecallCard(BaseModel):
    """A glass-box recall result — a ranked card WITH provenance and links.

    The structured, first-class ``recall`` shape (distinct from the injectable
    ``Recall``/``Bundle`` text): the agent-side ``recall`` tool surfaces these so a
    caller sees WHERE a memory came from (``source`` slug + ``kind`` + ``confidence``)
    and WHAT it points to (outbound ``[[links]]``), not just an opaque blob.
    ``verify_first`` carries the confidence gate (a ``guessed`` memory is surfaced
    tentatively).
    """

    source: str
    kind: str
    content: str
    score: float
    confidence: Confidence = Confidence.KNOWN
    classification: str = "unclassified"
    verify_first: bool = False
    #: WHEN the underlying memory was established (``YYYY-MM-DD``); empty when unstamped.
    established: str = ""
    provenance: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class Bundle(BaseModel):
    """The bounded, boundary-marked result of a single retrieval pass.

    ``text`` is the injectable rendering: each kept recall wrapped in a
    ``<memory-result>`` block and framed as untrusted DATA (never instructions).
    """

    recalls: list[Recall] = Field(default_factory=list)
    degraded: bool = False
    truncated: bool = False
    budget: int = 0
    text: str = ""


class ConsolidationResult(BaseModel):
    """Summary of one slow-path consolidation run (audit + observability)."""

    facts_updated: int = 0
    insights_minted: int = 0
    procedures_promoted: int = 0
    events_recorded: int = 0
    days_summarized: int = 0
    edges_decayed: int = 0
    files_rewritten: int = 0
    window_events: int = 0


class SourceRecord(BaseModel):
    """One normalized record pushed from a connected source, pre-routing (SPEC-073)."""

    external_id: str
    text: str
    kind: str = "observation"
    classification: str = "unclassified"
    #: ISO-8601 from the source system ("" = unknown); drives last-writer-wins order.
    source_updated_at: str = ""
    salience: float = 0.0
    metadata: dict[str, str] = Field(default_factory=dict)


class SourceMapping(BaseModel):
    """The operator-approved routing of a source to one-or-more homes."""

    source_id: str
    homes: list[str] = Field(default_factory=list)
    revision: str = ""
    content_hash: str = ""


class Provenance(BaseModel):
    """One source's claim on a canonical item (SPEC-073 COMP-011).

    The same bytes arriving from two sources dedup into
    ONE canonical item, but each source keeps its own ``classification`` --
    retrieval gates per-provenance, never on the item's highest label, so a
    stricter copy from one source cannot suppress a looser copy from another.
    """

    source: str
    external_id: str = ""
    classification: str = "unclassified"


class IngestResult(BaseModel):
    """Outcome of one ``ingest_batch`` call (audit + observability)."""

    ingested: int = 0
    skipped_stale: int = 0
    deduped: int = 0


__all__ = [
    "Bundle",
    "Confidence",
    "ConsolidationResult",
    "DaySummary",
    "Entity",
    "Event",
    "Fact",
    "IngestResult",
    "Insight",
    "LifeEvent",
    "Procedure",
    "Provenance",
    "Recall",
    "RecallCard",
    "Scope",
    "Situation",
    "SourceMapping",
    "SourceRecord",
    "TimeWindow",
    "utc_today",
]
