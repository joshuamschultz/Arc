"""T-020 — per-agent SQLite substrate: schema, guarded vec, hard isolation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.operator import MemoryOperator
from arcmemory.security import content_hash
from arcmemory.types import Event

requires_vec = pytest.mark.skipif(
    not sqlite_vec_loadable(),
    reason="sqlite-vec extension not loadable in this Python/SQLite build",
)


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


# -- H-REG-1: embedded_hash migration backfill (production hazard) -------------

_DIMS = 8
_EVENT_TEXT = "Ada owns the payments service"
_CHUNK_ID = "event:pre-existing"


def _write_pre_embedded_hash_db(workspace: Path) -> Path:
    """Hand-write ``chunks``/``vec0`` exactly as they shipped BEFORE H-REG-1 added
    ``embedded_hash`` — i.e. a real deployed agent's ``index.db``: a chunk row
    that already has a vector, and no ``embedded_hash`` column at all.
    """
    from arcmemory.db import _load_sqlite_vec

    db_path = workspace / "memory" / "index.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    assert _load_sqlite_vec(conn), "sqlite-vec must load for this migration scenario"
    conn.execute(
        "CREATE TABLE chunks ("
        "chunk_id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_path TEXT NOT NULL, "
        "mtime REAL, classification TEXT DEFAULT 'unclassified', content_hash TEXT)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE vec0 USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{_DIMS}])"
    )
    conn.execute(
        "INSERT INTO chunks (chunk_id, scope, source_path, mtime, classification, content_hash) "
        "VALUES (?, 'did:a', 'episodic', 0.0, 'unclassified', ?)",
        (_CHUNK_ID, content_hash(_EVENT_TEXT)),
    )
    import sqlite_vec

    conn.execute(
        "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)",
        (_CHUNK_ID, sqlite_vec.serialize_float32([0.1] * _DIMS)),
    )
    conn.commit()
    conn.close()
    return db_path


@requires_vec
def test_connect_backfills_embedded_hash_for_rows_that_already_have_a_vector(
    tmp_path: Path,
) -> None:
    """The migration hazard: embedded_hash starts NULL, but this row already has a
    vector. Connecting must stamp embedded_hash = content_hash for it immediately,
    so a background embed pass never mistakes it for unembedded content."""
    workspace = tmp_path / "agent-workspace"
    _write_pre_embedded_hash_db(workspace)

    memdb = MemoryDB(workspace, dims=_DIMS)
    conn = memdb.connect()

    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    assert "embedded_hash" in columns

    row = conn.execute(
        "SELECT content_hash, embedded_hash FROM chunks WHERE chunk_id = ?", (_CHUNK_ID,)
    ).fetchone()
    assert row == (content_hash(_EVENT_TEXT), content_hash(_EVENT_TEXT))


@requires_vec
def test_migration_backfill_is_idempotent_on_an_already_migrated_db(tmp_path: Path) -> None:
    """Reconnecting to an already-migrated DB must not error or re-run the backfill
    (the column already exists, so the one-time gate must not re-fire)."""
    workspace = tmp_path / "agent-workspace"
    _write_pre_embedded_hash_db(workspace)

    MemoryDB(workspace, dims=_DIMS).connect()  # first connect: migrates + backfills
    second = MemoryDB(workspace, dims=_DIMS)
    conn = second.connect()  # second connect: column already present, no-op

    row = conn.execute(
        "SELECT content_hash, embedded_hash FROM chunks WHERE chunk_id = ?", (_CHUNK_ID,)
    ).fetchone()
    assert row == (content_hash(_EVENT_TEXT), content_hash(_EVENT_TEXT))


@requires_vec
async def test_post_migration_embed_pass_does_not_re_embed_an_already_embedded_row(
    tmp_path: Path, embedder
) -> None:
    """The whole point of the backfill: after migrating a pre-existing embedded
    row, the next background embed pass must be a NO-OP for it — not a
    corpus-wide re-embed of months of already-embedded memory."""
    from arcmemory.index.surface import SurfaceIndex
    from arcmemory.stores.episodic import EpisodicStore
    from arcmemory.types import Scope

    workspace = tmp_path / "agent-workspace"
    _write_pre_embedded_hash_db(workspace)
    scope = Scope(agent_did="did:a")

    memdb = MemoryDB(workspace, dims=_DIMS)
    memdb.connect()  # migrates + backfills embedded_hash for the pre-existing row

    # The real source-of-truth episode this pre-existing chunk was derived from —
    # its content_hash must match what was hand-seeded above for the row to be
    # recognized as the SAME, already-resolved chunk on the real indexing path.
    EpisodicStore(memdb, workspace).append(
        Event(
            event_id="pre-existing",
            ts="2026-01-01T00:00:00+00:00",
            scope=scope.key,
            kind="obs",
            text=_EVENT_TEXT,
        )
    )

    surface = SurfaceIndex(memdb, workspace, scope, embedder=embedder)

    reindexed = await surface.index_if_needed(embed=True)

    assert reindexed == 0, "a chunk backfilled as already-embedded must not be re-selected"
    assert embedder.calls == 0, "the migration must not trigger a corpus-wide re-embed"
