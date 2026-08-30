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

from arctrust.audit import AuditSink, NullSink
from arctrust.classification import Classification, dominates, parse_classification
from pydantic import BaseModel, Field

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocHit
from arcmemory.index.backend import IndexBackend, open_index_backend
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.surface import SurfaceIndex, _fts_query
from arcmemory.retrieve import Retriever
from arcmemory.security import gate_no_read_up
from arcmemory.status import SemanticStatus
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.events import EventStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore, format_fact
from arcmemory.types import (
    DaySummary,
    Event,
    Insight,
    LifeEvent,
    Procedure,
    Provenance,
    Recall,
    Scope,
    Situation,
    SourceMapping,
)

#: Chunk text is capped independent of result count — a chunk browser must never
#: become a full-document dump (LLM02/threat-surface: sensitive info disclosure).
_CHUNK_TEXT_CAP = 500


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
    events: int = 0
    daily_notes: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0


class GraphNode(BaseModel):
    """One graph node, classification-gated before it ever leaves the operator (H-016).

    ``node_type`` mirrors :meth:`MemoryOperator._target_type` — ``entity`` (a
    backed semantic-store card) or ``cue`` (a bare co-occurrence term with no
    file). A cue carries no classification of its own, so it reads as
    ``unclassified`` (dominated by every clearance) rather than inventing a label.
    """

    id: str
    node_type: str  # "entity" | "cue"
    classification: str
    metadata: dict[str, object] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """One graph edge with the metadata ``neighbor_edges`` drops (H-016).

    Backed by :meth:`WeightedGraph.all_edges` so the viewer can render weight,
    salience, and recency for a hovered edge without a second query path.
    """

    src: str
    dst: str
    kind: str
    weight: float
    salience: float = 0.0
    last_hit: str | None = None
    hits: int = 0


class MemoryGraph(BaseModel):
    """A windowed neighborhood view of the associative graph (H-016).

    Never a whole-graph dump: :meth:`MemoryOperator.graph` caps both the hop
    radius and the node count server-side. An edge whose far endpoint failed the
    no-read-up gate (or fell outside the window) is dropped entirely — it leaves
    no trace in ``nodes``, in any node's metadata, or in a count/degree here.
    """

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


# H-016 hard ceiling on returned nodes, independent of any caller-supplied
# ``max_nodes`` — the graph endpoint is a neighborhood window, never a dump.
_GRAPH_HARD_MAX_NODES = 500


class ChunkRecord(BaseModel):
    """One indexed chunk (embedded and/or literal) with its metadata (H-023).

    ``text`` is capped to ``_CHUNK_TEXT_CAP`` chars regardless of how many
    results were returned — a chunk browser must never become a full-document
    dump; ``truncated`` says whether this record's text was cut. ``score`` is
    rank-derived (``1/(rank+1)``): ``IndexBackend`` returns ranked chunk ids,
    not comparable raw scores across bm25/vec/recency, so this is the one
    scale the browser can sort or display consistently.
    """

    chunk_id: str
    source: str  # source_path (file, event, or ingested document) this chunk came from
    scope: str
    classification: str
    mtime: float | None = None
    score: float
    text: str
    truncated: bool = False


class ChunkPage(BaseModel):
    """A gated, paginated page of an agent's chunks, newest first (H-023)."""

    items: list[ChunkRecord] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0


class ChunkSearchResult(BaseModel):
    """Chunk search result — literal (BM25) or vector, with an honest degrade flag.

    ``mode`` is the mode actually used, which may differ from what was
    requested: a ``"vector"`` request with no embedder/sqlite-vec wired
    degrades LOUD to ``"literal"`` (``degraded=True``) rather than raising or
    returning an empty result that looks like "no matches" (H-023).
    """

    items: list[ChunkRecord] = Field(default_factory=list)
    mode: str = "literal"
    degraded: bool = False
    query: str = ""


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
        audit_sink: AuditSink | None = None,
    ) -> None:
        if not agent_did:
            raise ValueError("MemoryOperator requires an agent_did (no memory without identity)")
        self._workspace = Path(workspace)
        self._agent_did = agent_did
        self._cfg = config or MemoryConfig()
        self._embedder = embedder
        self._seed_vocab = list(seed_vocabulary or [])
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._db = MemoryDB(self._workspace)
        self._graph = WeightedGraph(self._db, self._cfg)
        self._episodic = EpisodicStore(self._db, self._workspace)
        self._insights = InsightStore(self._workspace)
        self._procedures = ProceduralStore(self._workspace)
        self._events = EventStore(self._workspace)
        self._daily = DailyNotesStore(self._workspace)
        self._backend: IndexBackend = open_index_backend(self._cfg.index_backend, db=self._db)

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
            events=_md_count(mem / "events"),
            daily_notes=_md_count(mem / "daily-log"),
            graph_nodes=int(nodes),
            graph_edges=int(edges),
        )

    def graph(
        self,
        *,
        session_id: str | None = None,
        node: str | None = None,
        hops: int = 1,
        clearance: str = "unclassified",
        max_nodes: int = _GRAPH_HARD_MAX_NODES,
    ) -> MemoryGraph:
        """Windowed neighborhood view of the associative graph (H-016).

        With ``node`` set, walks up to ``hops`` undirected steps from it; with
        ``node=None``, returns a deterministic (sorted-id) slice of the whole
        scope. Either way the result is capped: ``hops`` never exceeds the
        tier's own spreading-activation cap (``MemoryConfig.max_hops``) and the
        node count never exceeds ``_GRAPH_HARD_MAX_NODES`` regardless of what a
        caller requests — this is a neighborhood window, never a whole-graph dump.

        Classification gating runs ONCE, here, through :meth:`_gated_node` — the
        same read path a hover/re-center call reuses by calling ``graph`` again.
        A node above ``clearance`` is dropped; an edge is kept only when BOTH
        endpoints survived the gate, so a hidden node leaves no trace anywhere
        in the response — not in ``nodes``, not in an edge, not in a count.
        """
        scope = self._scope(session_id)
        clr = parse_classification(clearance, strict=self._cfg.tier == "federal")
        hops = max(1, min(hops, self._cfg.max_hops))
        max_nodes = max(1, min(max_nodes, _GRAPH_HARD_MAX_NODES))
        raw_edges = self._graph.all_edges(scope.key)
        window = self._graph_window(raw_edges, node, hops, max_nodes)

        nodes: dict[str, GraphNode] = {}
        for node_id in window:
            record = self._gated_node(session_id, node_id, clr)
            if record is not None:
                nodes[node_id] = record

        edges = [
            GraphEdge(
                src=src,
                dst=dst,
                kind=kind,
                weight=weight,
                salience=salience,
                last_hit=last_hit,
                hits=hits,
            )
            for src, dst, kind, weight, salience, last_hit, hits in raw_edges
            if src in nodes and dst in nodes
        ]
        return MemoryGraph(nodes=list(nodes.values()), edges=edges)

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

    # -- connector-data views (SPEC-073 A1) ---------------------------------

    def list_sources(self, *, session_id: str | None = None) -> list[EntityRecord]:
        """Every registered ``source-<id>`` entity (``entity_type == 'source'``)."""
        return [e for e in self.list_entities(session_id=session_id) if e.entity_type == "source"]

    def get_source_mapping(
        self, source_id: str, *, session_id: str | None = None
    ) -> SourceMapping | None:
        """The committed home routing for one source (None if never committed)."""
        from arcmemory.mapping import load_committed_mapping

        return load_committed_mapping(source_id, store=self._semantic(session_id))

    def list_mappings(self, *, session_id: str | None = None) -> list[SourceMapping]:
        """``load_committed_mapping`` for every ``mapping-<id>`` entity."""
        from arcmemory.mapping import load_committed_mapping

        store = self._semantic(session_id)
        mappings: list[SourceMapping] = []
        for entity in self.list_entities(session_id=session_id):
            if entity.entity_type != "mapping" or not entity.slug.startswith("mapping-"):
                continue
            source_id = entity.slug[len("mapping-") :]
            mapping = load_committed_mapping(source_id, store=store)
            if mapping is not None:
                mappings.append(mapping)
        return mappings

    def list_blob_folders(
        self, source_id: str | None = None, *, session_id: str | None = None
    ) -> list[EntityRecord]:
        """``entity_type == 'blob_folder'``, optionally scoped to one source."""
        folders = [
            e for e in self.list_entities(session_id=session_id) if e.entity_type == "blob_folder"
        ]
        if source_id is None:
            return folders
        prefix = f"blob-{source_id}-"
        return [f for f in folders if f.slug.startswith(prefix)]

    def list_datastore_tables(self, *, session_id: str | None = None) -> list[EntityRecord]:
        """The introspected ``db-table-<name>`` ontology entities."""
        return [
            e for e in self.list_entities(session_id=session_id) if e.entity_type == "db_table"
        ]

    async def document_search(
        self, source_id: str, query: str, *, top_k: int = 10
    ) -> list[DocHit]:
        """Per-source document search, reusing the production doc-pool index."""
        from arcmemory.doc_index import DocIndex

        index = DocIndex(self._db, self._workspace, self._cfg, embedder=self._embedder)
        return await index.document_search(
            query, self._agent_did, source_id=source_id, top_k=top_k
        )

    async def list_documents(self, source_id: str, *, limit: int = 50) -> list[DocHit]:
        """Everything indexed for one source, newest first, with no query."""
        from arcmemory.doc_index import DocIndex

        index = DocIndex(self._db, self._workspace, self._cfg, embedder=self._embedder)
        return await index.list_documents(self._agent_did, source_id=source_id, limit=limit)

    def list_provenances(self, item_id: str) -> list[Provenance]:
        """Every provenance recorded against one canonical item."""
        from arcmemory.stores.provenance import ProvenanceStore

        return ProvenanceStore(self._db).provenances(item_id)

    async def index_health(self) -> SemanticStatus:
        """Honest semantic-channel probe (never fakes "live" without an embedder)."""
        from arcmemory.status import semantic_status

        return await semantic_status([self._workspace], embedder=self._embedder)

    def datastore_query(
        self,
        source_id: str,
        op: str,
        table: str,
        args: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> object | None:
        """Reopen the datastore READ-ONLY from its persisted ``datastore_path`` fact
        and run a typed read op. ``None`` when no path was ever persisted (e.g. an
        in-memory-only source) or datastore access is disabled -- degrade, not crash.
        """
        if not self._cfg.datastore_enabled:
            return None
        entity = self._semantic(session_id).read(f"source-{source_id}")
        if entity is None:
            return None
        fact = next((f for f in entity.facts if f.predicate == "sqlite_path"), None)
        if fact is None:
            return None
        import sqlite3

        from arcmemory.datastore import SqliteDatastore

        conn = sqlite3.connect(f"file:{fact.value}?mode=ro", uri=True)
        return SqliteDatastore(conn).query(op, table, args)

    def list_insights(self) -> list[Insight]:
        """Every minted insight card, sorted by id (the curated glass-box centerpiece)."""
        insights = [self._insights.read(iid) for iid in self._insights.all_ids()]
        return sorted((i for i in insights if i is not None), key=lambda i: i.id)

    def list_procedures(self) -> list[Procedure]:
        """Every how-to procedure card, sorted by slug."""
        procedures = [self._procedures.read(slug) for slug in self._procedures.slugs()]
        return sorted((p for p in procedures if p is not None), key=lambda p: p.slug)

    def list_events(self) -> list[LifeEvent]:
        """Every life-event card, most recent occurrence first (the user's timeline)."""
        events = [self._events.read(slug) for slug in self._events.slugs()]
        return sorted(
            (e for e in events if e is not None), key=lambda e: (e.date, e.slug), reverse=True
        )

    def list_daily_notes(self) -> list[DaySummary]:
        """Every day's curated notes, newest day first."""
        summaries = [self._daily.read(day) for day in self._daily.days()]
        return [s for s in summaries if s is not None]

    def read_daily_note(self, day: str) -> DaySummary | None:
        """Fetch one day's curated notes (None if absent)."""
        return self._daily.read(day)

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

    # -- chunks (H-023) ------------------------------------------------------

    async def browse_chunks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        clearance: str = "unclassified",
        session_id: str | None = None,
    ) -> ChunkPage:
        """One gated, paginated page of the recall-scope's chunks, newest first.

        The no-read-up gate runs BEFORE pagination: ``total`` and the page slice
        both reflect only what ``clearance`` may see, so a caller can never infer
        the existence of an over-clearance chunk from a shifted count.
        """
        scope = self._scope(session_id)
        await self._index_chunks(scope, embed=False)
        ids = await self._backend.recency_order(scope.key)
        recalls, meta = await self._hydrate_chunks(scope.key, ids)
        kept = self._gate_chunks(recalls, clearance)
        page = kept[offset : offset + limit]
        return ChunkPage(
            items=[self._to_chunk_record(r, meta, scope.key) for r in page],
            total=len(kept),
            limit=limit,
            offset=offset,
        )

    async def search_chunks(
        self,
        query: str,
        *,
        mode: str = "literal",
        limit: int = 10,
        clearance: str = "unclassified",
        session_id: str | None = None,
    ) -> ChunkSearchResult:
        """Search chunks by ``mode`` — ``"literal"`` (BM25) or ``"vector"`` (cosine).

        A ``"vector"`` request degrades LOUD to literal (``degraded=True``,
        ``mode="literal"``) when no embedder or sqlite-vec is available, or the
        embed call itself fails — this method never raises for that reason and
        never silently returns empty as if nothing matched. Every candidate is
        gated through the same no-read-up predicate ``retrieve.py`` uses, BEFORE
        ``limit`` is applied, so a dropped over-clearance chunk never displaces a
        visible one from the result. Any ``mode`` other than exactly ``"vector"``
        runs literal — there is no third, silently-empty mode.
        """
        scope = self._scope(session_id)
        want_vector = mode == "vector"
        await self._index_chunks(scope, embed=want_vector)

        degraded = False
        actual_mode = "vector" if want_vector else "literal"
        ids: list[str] = []
        if want_vector:
            vector_ready = self._backend.vec_available and self._embedder is not None
            vectors = (
                await embed_or_none(self._embedder, [query], operation="operator:search_chunks")
                if vector_ready
                else None
            )
            if vectors:
                ids = await self._backend.vec_search(scope.key, vectors[0])
            else:
                degraded = True
                actual_mode = "literal"
        if actual_mode == "literal":
            fts = _fts_query(query)
            ids = await self._backend.bm25_search(scope.key, fts) if fts else []

        recalls, meta = await self._hydrate_chunks(scope.key, ids)
        kept = self._gate_chunks(recalls, clearance)
        return ChunkSearchResult(
            items=[self._to_chunk_record(r, meta, scope.key) for r in kept[:limit]],
            mode=actual_mode,
            degraded=degraded,
            query=query,
        )

    async def _index_chunks(self, scope: Scope, *, embed: bool) -> None:
        """Freshen this scope's chunk/fts(/vec) rows before reading the backend.

        A separate ``SurfaceIndex`` per call, exactly the pattern :meth:`search`
        already uses for ``Retriever`` — indexing is incremental and content-
        gated, so a call with nothing new to index is nearly free (T-040).
        """
        surface = SurfaceIndex(
            self._db,
            self._workspace,
            scope,
            config=self._cfg,
            embedder=self._embedder,
            audit_sink=self._audit,
            seed_vocabulary=self._seed_vocab,
        )
        await surface.index_if_needed(embed=embed)

    async def _hydrate_chunks(
        self, scope_key: str, chunk_ids: list[str]
    ) -> tuple[list[Recall], dict[str, tuple[str, float | None]]]:
        """Turn ranked chunk ids into gate-ready ``Recall``s + their source/mtime.

        ``Recall`` is reused as the gate's input shape (it is exactly what
        ``gate_no_read_up`` accepts); ``source``/``mtime`` ride alongside in a
        side table since ``Recall.source`` is repurposed here to carry the chunk
        id, not the file path.
        """
        recalls: list[Recall] = []
        meta_by_id: dict[str, tuple[str, float | None]] = {}
        for rank, chunk_id in enumerate(chunk_ids):
            meta = await self._backend.chunk_meta(scope_key, chunk_id)
            text = await self._backend.chunk_text(scope_key, chunk_id)
            if meta is None or text is None:
                continue  # vanished between ranking and hydration — skip, don't fail
            source_path, classification, mtime = meta
            meta_by_id[chunk_id] = (source_path, mtime)
            recalls.append(
                Recall(
                    source=chunk_id,
                    content=text,
                    score=1.0 / (rank + 1),  # rank-derived (backend exposes no raw score)
                    kind="chunk",
                    classification=classification,
                )
            )
        return recalls, meta_by_id

    def _gate_chunks(self, recalls: list[Recall], clearance: str) -> list[Recall]:
        """Drop every chunk ``clearance`` does not dominate (reuses ``retrieve.py``'s gate)."""
        strict = self._cfg.tier == "federal"
        clr = parse_classification(clearance, strict=strict)
        return gate_no_read_up(
            recalls,
            clearance=clr,
            strict=strict,
            actor_did=self._agent_did,
            tier=self._cfg.tier,
            audit_sink=self._audit,
        )

    def _to_chunk_record(
        self,
        recall: Recall,
        meta_by_id: dict[str, tuple[str, float | None]],
        scope_key: str,
    ) -> ChunkRecord:
        source_path, mtime = meta_by_id.get(recall.source, (recall.source, None))
        truncated = len(recall.content) > _CHUNK_TEXT_CAP
        text = recall.content[:_CHUNK_TEXT_CAP] if truncated else recall.content
        return ChunkRecord(
            chunk_id=recall.source,
            source=source_path,
            scope=scope_key,
            classification=recall.classification,
            mtime=mtime,
            score=recall.score,
            text=text,
            truncated=truncated,
        )

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

    def _graph_window(
        self,
        raw_edges: list[tuple[str, str, str, float, float, str | None, int]],
        start: str | None,
        hops: int,
        max_nodes: int,
    ) -> list[str]:
        """Node ids in the requested window: a ``hops``-radius BFS from ``start``,
        or a deterministic capped slice of every node when ``start`` is None.
        """
        adjacency: dict[str, set[str]] = {}
        all_ids: set[str] = set()
        for src, dst, *_rest in raw_edges:
            adjacency.setdefault(src, set()).add(dst)
            adjacency.setdefault(dst, set()).add(src)
            all_ids.add(src)
            all_ids.add(dst)

        if start is None:
            return sorted(all_ids)[:max_nodes]

        visited = {start}
        frontier = {start}
        for _ in range(hops):
            frontier = {neighbor for n in frontier for neighbor in adjacency.get(n, set())}
            frontier -= visited
            if not frontier:
                break
            visited |= frontier
            if len(visited) >= max_nodes:
                break
        return sorted(visited)[:max_nodes]

    def _visible(self, label: str, clearance: Classification) -> bool:
        """No-read-up predicate for one classification label (fail-closed on unknown)."""
        try:
            resource = parse_classification(label, strict=self._cfg.tier == "federal")
        except ValueError:
            return False
        return dominates(clearance, resource)

    def _gated_node(
        self, session_id: str | None, node_id: str, clearance: Classification
    ) -> GraphNode | None:
        """Resolve + classification-gate one node id — the ONE read path both
        :meth:`graph` and any hover/re-center call (re-invoking ``graph``) share.

        Returns ``None`` when the node's classification does not dominate under
        ``clearance``; the caller drops it and every edge touching it, with no
        further trace (no id, no count, no degree) surviving in the response.
        """
        node_type = self._target_type(session_id, node_id)
        entity = self._semantic(session_id).read(node_id) if node_type == "entity" else None
        if entity is not None:
            if not self._visible(entity.classification, clearance):
                return None
            metadata: dict[str, object] = {
                "name": entity.name,
                "entity_type": entity.entity_type,
                "tags": entity.tags,
            }
            return GraphNode(
                id=node_id,
                node_type="entity",
                classification=entity.classification,
                metadata=metadata,
            )
        # A bare cue (co-occurrence term with no backing file) carries no
        # classification of its own — unclassified, dominated by every clearance.
        return GraphNode(id=node_id, node_type="cue", classification="unclassified", metadata={})

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
    "ChunkPage",
    "ChunkRecord",
    "ChunkSearchResult",
    "EntityRecord",
    "GraphEdge",
    "GraphNode",
    "LinkRecord",
    "MemoryGraph",
    "MemoryOperator",
    "MemoryPage",
    "MemoryRecord",
    "MemorySummary",
    "MutationResult",
    "MutationStatus",
]
