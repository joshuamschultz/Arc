"""T-020 — per-agent SQLite substrate: schema, guarded vec, hard isolation."""

from __future__ import annotations

from pathlib import Path

from arcmemory.db import MemoryDB


def _tables(memdb: MemoryDB) -> set[str]:
    conn = memdb.connect()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    return {r[0] for r in rows}


def test_index_db_created_in_agent_workspace(workspace: Path, db: MemoryDB) -> None:
    assert db.db_path == workspace / "memory" / "index.db"
    assert db.db_path.exists()


def test_core_tables_present(db: MemoryDB) -> None:
    tables = _tables(db)
    assert {"episodic", "chunks", "fts_chunks", "edges"} <= tables


def test_vec_extension_load_is_guarded(db: MemoryDB) -> None:
    # vec_available reflects whether the extension loaded; the vec0 table exists
    # iff it did. Either way, the DB opened without raising (the guard held).
    assert isinstance(db.vec_available, bool)
    assert ("vec0" in _tables(db)) == db.vec_available


def test_two_agents_are_separate_files(tmp_path: Path) -> None:
    a = MemoryDB(tmp_path / "agent-a")
    b = MemoryDB(tmp_path / "agent-b")
    a.connect()
    b.connect()
    assert a.db_path != b.db_path
    a.connect().execute(
        "INSERT INTO episodic (event_id, ts, scope, kind, text, seq) VALUES "
        "('e','t','did:a','k','secret',0)"
    )
    a.connect().commit()
    # Agent B's file has no visibility into agent A's rows — hard isolation.
    assert b.connect().execute("SELECT COUNT(*) FROM episodic").fetchone()[0] == 0


def test_wipe_derived_preserves_raw_stream(db: MemoryDB) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO episodic (event_id, ts, scope, kind, text, seq) VALUES "
        "('e','t','did:a','k','x',0)"
    )
    conn.execute("INSERT INTO fts_chunks (chunk_id, scope, text) VALUES ('c','did:a','x')")
    conn.commit()
    db.wipe_derived()
    assert conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] == 0
