"""T-025 — index is disposable: wipe -> rebuild is byte-identical + retrievable."""

from __future__ import annotations

from pathlib import Path

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import IndexRebuilder
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tagging import tag_entities
from arcmemory.types import Event, Scope

_VOCAB = ["alice", "bob"]


def _snapshot(db: MemoryDB) -> dict[str, list[tuple[object, ...]]]:
    conn = db.connect()
    snap: dict[str, list[tuple[object, ...]]] = {
        "fts": conn.execute("SELECT chunk_id, text FROM fts_chunks ORDER BY chunk_id").fetchall(),
        "edges": conn.execute(
            "SELECT scope, src, dst, kind, weight, salience, last_hit, hits "
            "FROM edges ORDER BY src, dst, kind"
        ).fetchall(),
    }
    if db.vec_available:
        snap["vec"] = conn.execute(
            "SELECT chunk_id, embedding FROM vec0 ORDER BY chunk_id"
        ).fetchall()
    return snap


def _seed_agent(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    """Create truth: an entity file (with a wiki-link) + a raw event stream."""
    semantic = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    semantic.write_fact("alice", "colleague", "[[bob]]", confidence=0.8)

    episodic = EpisodicStore(db, workspace)
    for i in range(3):
        text = f"alice and bob paired on task {i}"
        ev = Event(
            event_id=f"e{i}",
            ts=f"2026-07-07T00:00:0{i}+00:00",
            scope=scope.key,
            kind="obs",
            text=text,
            entities=tag_entities(text, _VOCAB),
        )
        episodic.append(ev)


async def test_wipe_rebuild_is_byte_identical(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
):
    _seed_agent(workspace, db, scope)
    rebuilder = IndexRebuilder(
        db, workspace, scope, config=MemoryConfig(), embedder=embedder, seed_vocabulary=_VOCAB
    )

    await rebuilder.rebuild()
    first = _snapshot(db)

    # rebuild wipes every derived table itself, so a second pass must reproduce the
    # first byte-for-byte (idempotent from any starting index state).
    await rebuilder.rebuild()
    second = _snapshot(db)

    assert first == second, "rebuild must reproduce every derived table identically"


async def test_rebuild_clears_orphaned_trigger_vectors(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
) -> None:
    _seed_agent(workspace, db, scope)
    conn = db.connect()
    # A poisoned/orphaned abstraction-space vector for an insight that no longer exists.
    conn.execute(
        "INSERT INTO insight_trigger (insight_id, scope, content_hash, embedding) "
        "VALUES (?, ?, ?, ?)",
        ("ghost", scope.key, "deadbeef", b"\x00\x00\x00\x00"),
    )
    conn.commit()

    await IndexRebuilder(db, workspace, scope, embedder=embedder, seed_vocabulary=_VOCAB).rebuild()

    # rebuild is the documented poison-fix path — the orphan must not survive it.
    assert conn.execute("SELECT COUNT(*) FROM insight_trigger").fetchone()[0] == 0


async def test_rebuild_produces_a_retrievable_set(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
):
    _seed_agent(workspace, db, scope)
    await IndexRebuilder(db, workspace, scope, embedder=embedder, seed_vocabulary=_VOCAB).rebuild()

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] > 0
    hits = conn.execute(
        "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'alice'"
    ).fetchall()
    assert hits, "FTS index must be queryable after rebuild"
    if db.vec_available:
        assert conn.execute("SELECT COUNT(*) FROM vec0").fetchone()[0] > 0


async def test_rebuild_reproduces_procedure_link_edges(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    """A procedure's graph node is truth-derived — a wipe+rebuild must restore it."""
    _seed_agent(workspace, db, scope)
    ProceduralStore(workspace).upsert(
        "seo-research", "SEO research", when_to_use="seo work", steps=["ask [[bob]] for the brief"]
    )

    await IndexRebuilder(db, workspace, scope, embedder=None, seed_vocabulary=_VOCAB).rebuild()

    assert "bob" in dict(WeightedGraph(db).neighbors(scope.key, "seo-research"))


async def test_rebuild_without_embedder_degrades(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    _seed_agent(workspace, db, scope)
    # No embedder injected -> vec table stays empty, but fts + edges still build.
    await IndexRebuilder(db, workspace, scope, embedder=None, seed_vocabulary=_VOCAB).rebuild()

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] > 0
    if db.vec_available:
        assert conn.execute("SELECT COUNT(*) FROM vec0").fetchone()[0] == 0


async def test_rebuild_of_one_scope_leaves_a_sibling_scopes_index_intact(
    workspace: Path, db: MemoryDB, embedder
) -> None:
    """A rebuild must wipe only its own scope, never every scope in the db.

    One agent's db holds several scopes at once — the bare recall scope plus a
    doc scope per connected source. rebuild() deleted chunks/fts/vec globally
    (no WHERE scope) but re-indexed only its own, so a rebuild for one scope
    silently emptied the others' caches — which forced the recall scope to
    re-embed its whole corpus on every lookup after any sibling rebuild ran.
    """
    from arcmemory.index.backend import open_index_backend

    # The recall scope (rebuilt from mem_dir + events) and a sibling doc scope
    # (a connected source's own pool, distinct chunk ids) — the real multi-scope
    # shape inside one agent's db.
    recall = Scope(agent_did="did:arc:agent")
    doc = Scope(agent_did="did:arc:agent", session_id="doc:dropbox")
    _seed_agent(workspace, db, recall)
    await IndexRebuilder(workspace=workspace, db=db, scope=recall, embedder=embedder).rebuild()

    backend = open_index_backend("sqlite", db=db)
    await backend.upsert_chunk(
        scope=doc.key,
        chunk_id="dropbox:report#0",
        source_path="dropbox://q3.txt",
        mtime=1000.0,
        classification="unclassified",
        content_hash="h",
        text="quarterly revenue report",
        embedding=[0.2] * db.dims if backend.vec_available else None,
    )

    def _chunk_count(scope: str) -> int:
        return (
            db.connect()
            .execute("SELECT count(*) FROM chunks WHERE scope=?", (scope,))
            .fetchone()[0]
        )

    assert _chunk_count(doc.key) == 1, "precondition: the doc scope was indexed"

    # Rebuilding the recall scope must not touch the doc scope.
    await IndexRebuilder(workspace=workspace, db=db, scope=recall, embedder=embedder).rebuild()

    assert _chunk_count(doc.key) == 1, "the doc scope's chunk was wiped by the recall rebuild"
    assert _chunk_count(recall.key) > 0, "the recall scope's own chunks are present"
