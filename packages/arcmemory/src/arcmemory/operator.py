"""Operator facade — the public read/mutation surface over an agent's memory DB.

This is the ONE seam arcui's Knowledge view consumes (SPEC arcui-reality-mirror,
COMP-001, REQ-084..REQ-100). It owns every store/graph/retriever access and returns
typed Pydantic records; no consumer runs SQL against ``index.db`` (REQ-087).

What it exposes for an agent's ``<workspace>/memory`` database:

* **list / get** episodic memories, paged, with the metadata a curator needs —
  created timestamp, a recency/decay indicator, an importance score on a 1..10
  scale, and the daily-log source reference (REQ-084);
* **entities** with their own confidence-derived metadata, and **links** between
  entities and memories so the operator can navigate the graph (REQ-085);
* **search** that delegates to arcmemory's own :class:`~arcmemory.retrieve.Retriever`
  so results are ranked exactly as production recall ranks them (REQ-086);
* **edit / set-metadata / delete** mutations that carry the actor DID for the audit
  trail arcui emits and return an honest :class:`MutationResult` — ``applied`` or
  ``error``, never a partial success (REQ-088, REQ-089, REQ-100).

The importance score is a faithful 1..10 projection of the entry's stored
``salience`` (the field that slows its decay); adjusting importance writes salience.
There is no invented score — a default-salience memory reads as importance ``1``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from arctrust.classification import parse_classification
from pydantic import BaseModel, Field

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder
from arcmemory.retrieve import Retriever
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import SemanticStore, format_fact
from arcmemory.types import Event, Recall, Scope, Situation


class MutationStatus(StrEnum):
    """Outcome of a facade mutation. There is no ``partial`` (REQ-089)."""

    APPLIED = "applied"
    ERROR = "error"


class MemoryRecord(BaseModel):
    """One episodic memory as the operator view sees it (REQ-084)."""

    entry_id: str
    scope: str
    kind: str
    text: str
    classification: str
    created: str
    salience: float
    importance: int  # 1..10 projection of salience (the decay-slowing field)
    recency: float  # 0..1 decay indicator: arcmemory's own curve applied to age
    source: str  # daily-log reference, relative to the workspace
    entities: list[str] = Field(default_factory=list)


class EntityRecord(BaseModel):
    """One semantic entity as the operator view sees it (REQ-084/085)."""

    slug: str
    name: str
    entity_type: str
    classification: str
    confidence: float
    importance: int  # 1..10 projection of confidence
    source: str
    links_to: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class LinkRecord(BaseModel):
    """A navigable edge from a memory or entity to a linked node (REQ-085)."""

    source_id: str
    target_id: str
    target_type: str  # "entity" | "cue"
    kind: str  # edge kind: "link" | "assoc" | "tagged"
    weight: float


class MemoryPage(BaseModel):
    """A page of episodic memories plus the totals needed to paginate (REQ-084)."""

    items: list[MemoryRecord] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0


class MutationResult(BaseModel):
    """The honest result of a single mutation — applied or error (REQ-089)."""

    status: MutationStatus
    operation: str
    actor_did: str
    entry_id: str | None = None
    error: str | None = None


class MemorySummary(BaseModel):
    """Counts for the knowledge overview — the DB stream, curated files, and graph."""

    episodic: int = 0
    entities: int = 0
    insights: int = 0
    procedures: int = 0
    daily_notes: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0


def _importance(scalar: float) -> int:
    """Project a 0..1 salience/confidence onto a 1..10 curator score."""
    return max(1, min(10, round(scalar * 10)))


def _md_count(directory: Path) -> int:
    """Number of ``.md`` cards in a curated store dir (0 if absent)."""
    return len(list(directory.glob("*.md"))) if directory.is_dir() else 0


class MemoryOperator:
    """Public read/mutation facade over one agent's memory database.

    Bound to an ``agent_did`` + workspace; a per-call ``session_id`` narrows the
    scope (shared-nothing isolation, LLM08). Read/mutation methods are synchronous
    SQLite operations; :meth:`search` is async because it drives the retriever.
    """

    def __init__(
        self,
        workspace: Path | str,
        agent_did: str,
        *,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        seed_vocabulary: Iterable[str] | None = None,
    ) -> None:
        if not agent_did:
            raise ValueError("MemoryOperator requires an agent_did (no memory without identity)")
        self._workspace = Path(workspace)
        self._agent_did = agent_did
        self._cfg = config or MemoryConfig()
        self._embedder = embedder
        self._seed_vocab = list(seed_vocabulary or [])
        self._db = MemoryDB(self._workspace)
        self._graph = WeightedGraph(self._db, self._cfg)
        self._episodic = EpisodicStore(self._db, self._workspace)

    # -- reads -------------------------------------------------------------

    def list_entries(
        self, *, limit: int = 50, offset: int = 0, session_id: str | None = None
    ) -> MemoryPage:
        """Return one page of episodic memories, newest first (REQ-084)."""
        scope = self._scope(session_id)
        events = self._episodic.page(scope.key, limit=limit, offset=offset)
        return MemoryPage(
            items=[self._to_record(scope.key, ev) for ev in events],
            total=self._episodic.count(scope.key),
            limit=limit,
            offset=offset,
        )

    def get_entry(self, entry_id: str, *, session_id: str | None = None) -> MemoryRecord | None:
        """Fetch a single episodic memory (None if absent)."""
        scope = self._scope(session_id)
        event = self._episodic.get(scope.key, entry_id)
        return self._to_record(scope.key, event) if event is not None else None

    def summary(self, *, session_id: str | None = None) -> MemorySummary:
        """Aggregate counts for the knowledge overview (REQ-084): stream + files + graph."""
        scope = self._scope(session_id)
        mem = self._workspace / "memory"
        conn = self._db.connect()
        (edges,) = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE scope = ?", (scope.key,)
        ).fetchone()
        (nodes,) = conn.execute(
            "SELECT COUNT(*) FROM (SELECT src FROM edges WHERE scope = ? "
            "UNION SELECT dst FROM edges WHERE scope = ?)",
            (scope.key, scope.key),
        ).fetchone()
        return MemorySummary(
            episodic=self._episodic.count(scope.key),
            entities=_md_count(mem / "entities"),
            insights=_md_count(mem / "insights"),
            procedures=_md_count(mem / "procedures"),
            daily_notes=_md_count(mem / "daily-log"),
            graph_nodes=int(nodes),
            graph_edges=int(edges),
        )

    def list_entities(self, *, session_id: str | None = None) -> list[EntityRecord]:
        """Return every semantic entity with its metadata (REQ-084)."""
        store = self._semantic(session_id)
        records: list[EntityRecord] = []
        for slug in store.slugs():
            entity = store.read(slug)
            if entity is None:
                continue
            records.append(
                EntityRecord(
                    slug=entity.slug,
                    name=entity.name,
                    entity_type=entity.entity_type,
                    classification=entity.classification,
                    confidence=entity.confidence,
                    importance=_importance(entity.confidence),
                    source=f"memory/entities/{entity.slug}.md",
                    links_to=entity.links_to,
                    facts=[format_fact(fact) for fact in entity.facts],
                    tags=entity.tags,
                )
            )
        return records

    def get_entity(self, slug: str, *, session_id: str | None = None) -> EntityRecord | None:
        """Fetch a single entity record (None if absent)."""
        return next((e for e in self.list_entities(session_id=session_id) if e.slug == slug), None)

    def links(self, node_id: str, *, session_id: str | None = None) -> list[LinkRecord]:
        """Linked entities/memories for a memory or entity, navigable (REQ-085).

        A memory's links are its tagged entities; an entity's links are its graph
        neighbors (wiki edges + reinforced co-occurrence), each carrying the edge
        kind and weight so the caller can render why the two are linked.
        """
        scope = self._scope(session_id)
        memory = self._episodic.get(scope.key, node_id)
        if memory is not None:
            return [
                LinkRecord(
                    source_id=node_id,
                    target_id=slug,
                    target_type=self._target_type(session_id, slug),
                    kind="tagged",
                    weight=self._graph.weight(scope.key, node_id, slug),
                )
                for slug in memory.entities
            ]
        return [
            LinkRecord(
                source_id=node_id,
                target_id=target,
                target_type=self._target_type(session_id, target),
                kind=kind,
                weight=weight,
            )
            for target, kind, weight in self._graph.neighbor_edges(scope.key, node_id)
        ]

    async def search(
        self,
        query: str,
        *,
        clearance: str = "unclassified",
        top_k: int = 5,
        budget: int = 1024,
        session_id: str | None = None,
    ) -> list[Recall]:
        """Ranked recall for ``query``, delegating to the production Retriever (REQ-086)."""
        scope = self._scope(session_id)
        retriever = Retriever(
            self._db,
            self._workspace,
            scope,
            config=self._cfg,
            embedder=self._embedder,
            seed_vocabulary=self._seed_vocab,
        )
        await retriever.index()
        clr = parse_classification(clearance, strict=self._cfg.tier == "federal")
        bundle = await retriever.retrieve(
            Situation(text=query), clearance=clr, top_k=top_k, budget=budget
        )
        return bundle.recalls

    # -- mutations ---------------------------------------------------------

    def edit_entry(
        self, entry_id: str, text: str, *, actor_did: str, session_id: str | None = None
    ) -> MutationResult:
        """Replace a memory entry's text (REQ-088)."""
        return self._mutate(
            "edit",
            entry_id,
            actor_did,
            lambda scope: self._episodic.update_text(scope.key, entry_id, text),
        )

    def set_metadata(
        self,
        entry_id: str,
        *,
        actor_did: str,
        importance: int | None = None,
        salience: float | None = None,
        session_id: str | None = None,
    ) -> MutationResult:
        """Adjust a memory entry's importance / decay-relevant salience (REQ-100).

        ``importance`` (1..10) and ``salience`` (0..1) are two views of the same
        stored field; pass either. ``importance`` maps to ``salience = importance/10``.
        """
        if importance is None and salience is None:
            return MutationResult(
                status=MutationStatus.ERROR,
                operation="set_metadata",
                actor_did=actor_did,
                entry_id=entry_id,
                error="set_metadata requires importance or salience",
            )
        value = salience if salience is not None else max(1, min(10, int(importance or 0))) / 10.0
        return self._mutate(
            "set_metadata",
            entry_id,
            actor_did,
            lambda scope: self._episodic.update_salience(scope.key, entry_id, value),
        )

    def delete_entry(
        self, entry_id: str, *, actor_did: str, session_id: str | None = None
    ) -> MutationResult:
        """Delete a memory entry (REQ-088)."""
        return self._mutate(
            "delete",
            entry_id,
            actor_did,
            lambda scope: self._episodic.delete(scope.key, entry_id),
            session_id=session_id,
        )

    # -- internals ---------------------------------------------------------

    def _mutate(
        self,
        operation: str,
        entry_id: str,
        actor_did: str,
        apply: Callable[[Scope], bool],
        *,
        session_id: str | None = None,
    ) -> MutationResult:
        """Run one atomic store op, returning an honest applied|error result.

        A store exception surfaces verbatim; a no-op (missing entry) is an error, not
        a silent success — so the caller never mistakes "nothing happened" for done.
        """
        scope = self._scope(session_id)
        try:
            affected = apply(scope)
        except Exception as exc:  # surface any store failure verbatim (REQ-089)
            return MutationResult(
                status=MutationStatus.ERROR,
                operation=operation,
                actor_did=actor_did,
                entry_id=entry_id,
                error=str(exc),
            )
        if not affected:
            return MutationResult(
                status=MutationStatus.ERROR,
                operation=operation,
                actor_did=actor_did,
                entry_id=entry_id,
                error=f"entry {entry_id!r} not found",
            )
        return MutationResult(
            status=MutationStatus.APPLIED,
            operation=operation,
            actor_did=actor_did,
            entry_id=entry_id,
        )

    def _scope(self, session_id: str | None) -> Scope:
        return Scope(agent_did=self._agent_did, session_id=session_id)

    def _semantic(self, session_id: str | None) -> SemanticStore:
        return SemanticStore(self._workspace, self._graph, self._scope(session_id).key)

    def _target_type(self, session_id: str | None, slug: str) -> str:
        """A link target is an ``entity`` if it has a file, else a bare graph ``cue``."""
        return "entity" if self._semantic(session_id).path_for(slug).exists() else "cue"

    def _to_record(self, scope_key: str, event: Event) -> MemoryRecord:
        return MemoryRecord(
            entry_id=event.event_id,
            scope=scope_key,
            kind=event.kind,
            text=event.text,
            classification=event.classification,
            created=event.ts,
            salience=event.salience,
            importance=_importance(event.salience),
            recency=self._recency(event.ts),
            source=f"memory/daily-log/{event.ts[:10]}.md",
            entities=event.entities,
        )

    def _recency(self, created: str) -> float:
        """Freshness in 0..1 via arcmemory's own decay curve on the entry's age.

        ``e^(-lambda_fast * age_days)`` — the same forgetting curve the graph applies
        to edges — so a just-captured memory reads ~1.0 and an old one decays toward 0.
        """
        try:
            ts = datetime.fromisoformat(created)
        except ValueError:
            return 0.0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age_days = max(0.0, (datetime.now(UTC) - ts).total_seconds() / 86400.0)
        return math.exp(-self._cfg.lambda_fast * age_days)


__all__ = [
    "EntityRecord",
    "LinkRecord",
    "MemoryOperator",
    "MemoryPage",
    "MemoryRecord",
    "MemorySummary",
    "MutationResult",
    "MutationStatus",
]
