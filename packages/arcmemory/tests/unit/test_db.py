"""T-020 — per-agent SQLite substrate: schema, guarded vec, hard isolation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.operator import MemoryOperator
from arcmemory.types import Event


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


# -- Task 37: self-migration for columns added after a table already exists ----


def _write_pre_salience_db(workspace: Path) -> Path:
    """Hand-write the episodic table exactly as it shipped BEFORE T-702/703
    added ``salience``/``entities`` — i.e. the schema an already-deployed
    agent's ``index.db`` has on disk right now. ``CREATE TABLE IF NOT EXISTS``
    no-ops against this file, so opening it through unmigrated code raises
    ``OperationalError: no such column: salience`` on every capture/recall.
    """
    db_path = workspace / "memory" / "index.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE episodic ("
        "event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, scope TEXT NOT NULL, "
        "kind TEXT NOT NULL, text TEXT NOT NULL, hash TEXT, "
        "classification TEXT DEFAULT 'unclassified', refs TEXT, seq INTEGER)"
    )
    conn.execute(
        "INSERT INTO episodic (event_id, ts, scope, kind, text, seq) VALUES "
        "('pre-existing', 't0', 'did:a', 'k', 'a memory captured before salience', 0)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_connect_migrates_pre_salience_episodic_table(tmp_path: Path) -> None:
    """Opening a DB predating T-702/703 must add salience/entities in place —
    no data loss, the pre-existing row survives with the column defaults."""
    workspace = tmp_path / "agent-workspace"
    _write_pre_salience_db(workspace)

    memdb = MemoryDB(workspace, dims=8)
    conn = memdb.connect()

    columns = {row[1] for row in conn.execute("PRAGMA table_info(episodic)")}
    assert {"salience", "entities"} <= columns

    row = conn.execute(
        "SELECT event_id, salience, entities FROM episodic WHERE event_id = 'pre-existing'"
    ).fetchone()
    assert row == ("pre-existing", 0.0, None)


def test_capture_and_facade_work_after_migration(tmp_path: Path) -> None:
    """The failure mode task 37 closes: capture (INSERT ... salience, entities)
    and the MemoryOperator facade (list_entries, set_metadata) both raised
    OperationalError on a pre-salience DB before the migration existed."""
    workspace = tmp_path / "agent-workspace"
    _write_pre_salience_db(workspace)

    memdb = MemoryDB(workspace, dims=8)
    memdb.connect()

    from arcmemory.stores.episodic import EpisodicStore

    store = EpisodicStore(memdb, workspace)
    store.append(
        Event(event_id="new-event", scope="did:a", kind="respond", text="captured post-migration")
    )

    operator = MemoryOperator(workspace, agent_did="did:a")
    page = operator.list_entries()
    ids = {item.entry_id for item in page.items}
    assert {"pre-existing", "new-event"} <= ids

    result = operator.set_metadata("new-event", actor_did="did:a", importance=8)
    assert result.status.value == "applied"
