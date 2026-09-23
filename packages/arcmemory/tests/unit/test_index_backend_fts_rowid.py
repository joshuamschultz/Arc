"""The SQLite text index is addressed by row, never scanned per chunk.

``fts_chunks`` is an FTS5 table whose ``chunk_id``/``scope`` columns are
UNINDEXED. Every ``WHERE chunk_id=?`` against it read the whole table: one
upsert or one object delete cost a full pass over every indexed chunk's text.
On a live box with a 2-3 GB index that was several GB of reads per ingested
object — the 90 TB of ``pread`` that pinned the event loop. Each chunk row now
carries the rowid of its text row, and writes, deletes and point reads go
through it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.index.backend import SqliteIndexBackend

_SCOPE = "did:arc:agent:doc:source"
_TEXT = "word " * 400


async def _seed(backend: SqliteIndexBackend, count: int) -> None:
    for number in range(count):
        await backend.upsert_chunk(
            scope=_SCOPE,
            chunk_id=f"object-{number:05d}#0",
            source_path=f"doc-{number}.md",
            mtime=1.0,
            classification="unclassified",
            content_hash=f"h{number}",
            text=f"{_TEXT} unique{number}",
            embedding=None,
        )


@contextmanager
def _vm_steps(conn: sqlite3.Connection) -> Iterator[list[int]]:
    """Count SQLite virtual-machine steps (in hundreds) spent inside the block."""
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(tick, 100)
    try:
        yield steps
    finally:
        conn.set_progress_handler(None, 0)


async def _cost_of_one_object(workspace: Path, existing: int) -> int:
    db = MemoryDB(workspace, dims=8)
    backend = SqliteIndexBackend(db)
    await _seed(backend, existing)
    with _vm_steps(db.connect()) as steps:
        await backend.delete_object(_SCOPE, "target")
        await backend.upsert_chunk(
            scope=_SCOPE,
            chunk_id="target#0",
            source_path="target.md",
            mtime=1.0,
            classification="unclassified",
            content_hash="t1",
            text="target text",
            embedding=None,
        )
        await backend.upsert_chunk(
            scope=_SCOPE,
            chunk_id="target#0",
            source_path="target.md",
            mtime=2.0,
            classification="unclassified",
            content_hash="t2",
            text="target text changed",
            embedding=None,
        )
        assert await backend.chunk_text(_SCOPE, "target#0") == "target text changed"
        await backend.delete_object(_SCOPE, "target")
    db.close()
    return steps[0]


async def test_one_object_costs_the_same_whatever_the_index_holds(tmp_path: Path) -> None:
    small = await _cost_of_one_object(tmp_path / "small", 10)
    large = await _cost_of_one_object(tmp_path / "large", 1500)

    assert large <= small * 2 + 20, (small, large)


async def test_replaced_and_deleted_chunks_leave_no_text_rows_behind(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path, dims=8)
    backend = SqliteIndexBackend(db)
    await _seed(backend, 3)
    await _seed(backend, 3)  # same ids again: replace, never duplicate
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] == 3

    await backend.delete_object(_SCOPE, "object-00001")

    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] == 2
    assert await backend.chunk_text(_SCOPE, "object-00001#0") is None
    assert await backend.bm25_search(_SCOPE, "unique1") == []


async def test_collection_index_windows_are_deleted_with_the_index(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path, dims=8)
    backend = SqliteIndexBackend(db)
    for chunk_id in ("index:source", "index:source#1", "index:sourcex#0"):
        await backend.upsert_chunk(
            scope=_SCOPE,
            chunk_id=chunk_id,
            source_path="index.md",
            mtime=1.0,
            classification="unclassified",
            content_hash=chunk_id,
            text=chunk_id,
            embedding=None,
        )

    await backend.delete_object(_SCOPE, "index:source")

    assert await backend.chunk_text(_SCOPE, "index:source") is None
    assert await backend.chunk_text(_SCOPE, "index:source#1") is None
    assert await backend.chunk_text(_SCOPE, "index:sourcex#0") == "index:sourcex#0"


def _legacy_database(workspace: Path) -> None:
    """A pre-fix index.db: chunk rows with no link to their text rows."""
    path = workspace / "memory" / "index.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
        "source_path TEXT NOT NULL, mtime REAL, classification TEXT DEFAULT 'unclassified', "
        "content_hash TEXT, embedded_hash TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE fts_chunks USING fts5(chunk_id UNINDEXED, scope UNINDEXED, text)"
    )
    for chunk_id in ("a#0", "b#0"):
        conn.execute(
            "INSERT INTO chunks (chunk_id, scope, source_path, content_hash) VALUES (?, ?, ?, ?)",
            (chunk_id, _SCOPE, "p", "h"),
        )
        conn.execute(
            "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
            (chunk_id, _SCOPE, f"text of {chunk_id}"),
        )
    # A text row whose chunk row is gone: an older delete missed it.
    conn.execute(
        "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
        ("gone#0", _SCOPE, "orphaned text"),
    )
    conn.commit()
    conn.close()


async def test_an_existing_database_is_linked_once_when_opened(tmp_path: Path) -> None:
    _legacy_database(tmp_path)
    db = MemoryDB(tmp_path, dims=8)
    conn = db.connect()
    backend = SqliteIndexBackend(db)

    linked = conn.execute("SELECT COUNT(*) FROM chunks WHERE fts_rowid IS NOT NULL").fetchone()[0]
    assert linked == 2
    assert await backend.bm25_search(_SCOPE, "orphaned") == []
    assert await backend.chunk_text(_SCOPE, "a#0") == "text of a#0"

    await backend.delete_object(_SCOPE, "a")

    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] == 1
    db.close()
    # Opening again finds nothing left to link and changes nothing.
    reopened = MemoryDB(tmp_path, dims=8)
    assert reopened.connect().execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] == 1
    reopened.close()
