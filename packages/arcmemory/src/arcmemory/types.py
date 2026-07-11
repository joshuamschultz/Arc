"""Typed data models for arcmemory (Pydantic v2).

Every value that crosses a store, index, or retrieval boundary is one of these
models. They are the contract the ``Brain`` Protocol speaks; keeping them here
(and free of any I/O) is what lets the stores stay swappable and the whole
package type-check under ``mypy --strict``.

Two vocabularies worth stating once:

* **Scope** — the shared-nothing isolation key. Every capture, every recall, every
  edge belongs to exactly one ``Scope`` (an agent DID, optionally narrowed to a
  session). No cross-scope table ever holds another scope's plaintext (LLM08).
* **confidence / salience** — the two scalars that drive FERNme dynamics.
  ``confidence`` grows with corroboration (``1 - e^(-gamma*hits)``) and separates a
  ``guessed`` memory (verify first) from a ``known`` one (actionable). ``salience``
  slows forgetting so a rare-but-significant signal survives decay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string (the canonical ts format)."""
    return datetime.now(UTC).isoformat()


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


class Fact(BaseModel):
    """A compact semantic fact-triplet about an entity.

    Rendered to markdown as ``predicate: value .confidence date`` with an optional
    ``| was: prior .conf`` contradiction trail (additive, never destructive).
    """

    predicate: str
    value: str
    confidence: float = 0.5
    date: str = Field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d"))
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


class Procedure(BaseModel):
    """A how-to card — a repeatable process, findable by its trigger.

    ``when_to_use`` is the situation to match against later (kept searchable so the
    right procedure surfaces when a similar task recurs). ``steps`` may carry
    ``[[slug]]`` links to the entities/tools they involve.
    """

    slug: str
    title: str
    when_to_use: str = ""
    steps: list[str] = Field(default_factory=list)
    use_count: int = 0
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
    days_summarized: int = 0
    edges_decayed: int = 0
    files_rewritten: int = 0
    window_events: int = 0


__all__ = [
    "Bundle",
    "Confidence",
    "ConsolidationResult",
    "DaySummary",
    "Entity",
    "Event",
    "Fact",
    "Insight",
    "Procedure",
    "Recall",
    "Scope",
    "Situation",
    "TimeWindow",
]
