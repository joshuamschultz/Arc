"""T-025 — index is disposable: wipe -> rebuild is byte-identical + retrievable."""

from __future__ import annotations

from pathlib import Path

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import IndexRebuilder
from arcmemory.stores.episodic import EpisodicStore
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
        episodic.append_bullet(ev)


def test_wipe_rebuild_is_byte_identical(workspace: Path, db: MemoryDB, scope: Scope, embedder):
    _seed_agent(workspace, db, scope)
    rebuilder = IndexRebuilder(
        db, workspace, scope, config=MemoryConfig(), embedder=embedder, seed_vocabulary=_VOCAB
    )

    rebuilder.rebuild()
    first = _snapshot(db)

    db.wipe_derived()
    rebuilder.rebuild()
    second = _snapshot(db)

    assert first == second, "rebuild must reproduce every derived table identically"


def test_rebuild_produces_a_retrievable_set(workspace: Path, db: MemoryDB, scope: Scope, embedder):
    _seed_agent(workspace, db, scope)
    IndexRebuilder(db, workspace, scope, embedder=embedder, seed_vocabulary=_VOCAB).rebuild()

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] > 0
    hits = conn.execute(
        "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH 'alice'"
    ).fetchall()
    assert hits, "FTS index must be queryable after rebuild"
    if db.vec_available:
        assert conn.execute("SELECT COUNT(*) FROM vec0").fetchone()[0] > 0


def test_rebuild_without_embedder_degrades(workspace: Path, db: MemoryDB, scope: Scope) -> None:
    _seed_agent(workspace, db, scope)
    # No embedder injected -> vec table stays empty, but fts + edges still build.
    IndexRebuilder(db, workspace, scope, embedder=None, seed_vocabulary=_VOCAB).rebuild()

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] > 0
    if db.vec_available:
        assert conn.execute("SELECT COUNT(*) FROM vec0").fetchone()[0] == 0
