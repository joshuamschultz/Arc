"""H-REG-1 — the surface index's embed/lexical split, and the hash-gate trap.

The regression: ``Brain.retrieve(index=False)`` (the agent recall hot path) used
to skip ``SurfaceIndex.index_if_needed`` ENTIRELY, so a just-captured card was
invisible even to lexical (BM25) search until a background embed pass ran — that
broke arcmemory's own degrade contract ("no embedder -> BM25 + graph must still
work") and six real-path journey tests.

The fix splits ``index_if_needed`` into two costs: the CHEAP lexical write (chunk
+ fts/BM25 row, zero LLM) always runs for changed content; the EXPENSIVE vector
embed only runs when ``embed=True``. That split creates a trap of its own: if a
chunk is written lexically (embed=False, its ``content_hash`` stamped, no vector)
a LATER ``embed=True`` pass gating solely on ``content_hash`` would see "nothing
changed" and never embed it. ``embedded_hash`` (a separate, vector-specific hash
column) is what lets the embed pass tell "written lexically, never embedded"
apart from "already embedded, nothing to do" — these tests pin that contract
directly against the real ``SurfaceIndex`` + ``SqliteIndexBackend`` path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.index.backend import open_index_backend
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope

requires_vec = pytest.mark.skipif(
    not sqlite_vec_loadable(),
    reason="sqlite-vec extension not loadable in this Python/SQLite build",
)


def _append(episodic: EpisodicStore, scope: Scope, eid: str, text: str, ts: str) -> None:
    episodic.append(Event(event_id=eid, ts=ts, scope=scope.key, kind="obs", text=text))


def _row_count(db: MemoryDB, table: str, chunk_id: str) -> int:
    conn = db.connect()
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE chunk_id=?",  # noqa: S608 - table is a fixed literal
        (chunk_id,),
    ).fetchone()
    return int(row[0])


# -- lexical write is synchronous and zero-LLM ------------------------------


async def test_embed_false_writes_the_lexical_row_without_calling_the_embedder(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
) -> None:
    """A just-captured card is BM25-searchable the SAME call, at zero embed cost."""
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "the payments service is owned by ada", "2026-08-01T00:00:00Z")

    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)
    written = await surface.index_if_needed(embed=False)

    assert written == 1
    assert embedder.calls == 0, "embed=False must never call the embedder (H-REG-1)"

    result = await surface.search("payments", top_k=3)
    assert any("payments" in r.content for r in result.recalls), (
        "the lexical write must make the card BM25-searchable immediately"
    )

    backend = open_index_backend("sqlite", db=db)
    assert await backend.embedded_hashes(scope.key) == {}, (
        "a lexical-only write must not claim a vector was ever written"
    )


# -- the hash-gate trap: a lexically-written chunk gets embedded later -------


@requires_vec
async def test_background_embed_recovers_a_chunk_written_lexically_only(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
) -> None:
    """The trap: gating an embed pass on content_hash ALONE would starve this chunk.

    Its content_hash never changes between the lexical write and the later embed
    pass, so a naive "changed since last stored hash" gate sees nothing to do and
    the vector would NEVER be written. The fix tracks ``embedded_hash`` apart from
    ``content_hash`` precisely so this pass still finds and embeds it.
    """
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "the puppy barked at the mailman", "2026-08-01T00:00:00Z")
    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)

    await surface.index_if_needed(embed=False)  # lexical only — content_hash stamped
    assert embedder.calls == 0

    embedded_count = await surface.index_if_needed(embed=True)

    assert embedded_count == 1, "the lexically-written chunk must be picked up for embedding"
    assert embedder.calls == 1

    backend = open_index_backend("sqlite", db=db)
    hashes = await backend.stored_hashes(scope.key)
    embedded = await backend.embedded_hashes(scope.key)
    assert embedded.get("event:e0") == hashes["event:e0"], (
        "embedded_hash must now match content_hash — the vector is for CURRENT content"
    )
    self_vector = (await embedder.embed_texts(["the puppy barked at the mailman"]))[0]
    assert await backend.vec_search(scope.key, self_vector) == ["event:e0"], (
        "the vector must actually be queryable now, not merely recorded as stamped"
    )


@requires_vec
async def test_mutation_delete_the_pending_embed_branch_and_watch_it_go_red(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
) -> None:
    """Same scenario as above, phrased as a standalone assertion for the mutation check.

    Deleting the ``chasing_pending_embeds`` OR-branch in
    ``SurfaceIndex.index_if_needed`` (so the embed pass gates on ``content_hash``
    alone, exactly like the pre-fix code) makes ``embedded_count`` come back ``0``
    and ``embedder.calls`` stay ``0`` here — this assertion is what goes RED under
    that mutation (see the task report for the manual mutation run).
    """
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "the kestrel deployment root is xyzzy", "2026-08-01T00:00:00Z")
    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)

    await surface.index_if_needed(embed=False)
    embedded_count = await surface.index_if_needed(embed=True)

    assert embedded_count == 1
    assert embedder.calls == 1


# -- idempotence (a): the embed pass updates the SAME row, never duplicates -


@requires_vec
async def test_embed_pass_updates_the_same_row_in_place_no_duplicates(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
) -> None:
    episodic = EpisodicStore(db, workspace)
    _append(
        episodic, scope, "e0", "the falcon mission status is greenlight", "2026-08-01T00:00:00Z"
    )
    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)

    await surface.index_if_needed(embed=False)  # lexical write
    await surface.index_if_needed(embed=True)  # embed pass recovers it

    assert _row_count(db, "chunks", "event:e0") == 1
    assert _row_count(db, "fts_chunks", "event:e0") == 1
    assert _row_count(db, "vec0", "event:e0") == 1

    # A third, no-op pass (nothing changed) touches nothing further and embeds again.
    third = await surface.index_if_needed(embed=True)
    assert third == 0
    assert embedder.calls == 1
    assert _row_count(db, "chunks", "event:e0") == 1
    assert _row_count(db, "fts_chunks", "event:e0") == 1
    assert _row_count(db, "vec0", "event:e0") == 1


# -- idempotence (b): a lexical rewrite of the SAME chunk refreshes fts, no dupes


async def test_lexical_rewrite_refreshes_the_fts_row_without_leaving_a_stale_one(
    workspace: Path, db: MemoryDB, scope: Scope, embedder
) -> None:
    """Editing a card's content and re-indexing (embed=False) must not leave a
    stale lexical row alongside the fresh one — the old text must be gone."""
    entities = workspace / "memory" / "entities"
    entities.mkdir(parents=True)
    card = entities / "rex.md"
    card.write_text("---\ntype: entity\nname: Rex\n---\n\nORIGINAL_MARKER_ONE")

    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)
    await surface.index_if_needed(embed=False)
    chunk_id = "file:memory/entities/rex.md"
    assert _row_count(db, "fts_chunks", chunk_id) == 1

    card.write_text("---\ntype: entity\nname: Rex\n---\n\nUPDATED_MARKER_TWO")
    changed = await surface.index_if_needed(embed=False)

    assert changed == 1
    assert embedder.calls == 0
    assert _row_count(db, "fts_chunks", chunk_id) == 1, "must update in place, never duplicate"

    fresh = await surface.search("UPDATED_MARKER_TWO", top_k=3)
    assert any("UPDATED_MARKER_TWO" in r.content for r in fresh.recalls)
    stale = await surface.search("ORIGINAL_MARKER_ONE", top_k=3)
    assert not any("ORIGINAL_MARKER_ONE" in r.content for r in stale.recalls), (
        "the stale lexical row must be gone, not merely outranked"
    )


# -- degrade never wastes work when embedding is impossible ------------------


async def test_embed_true_with_no_embedder_wired_does_not_reindex_unchanged_chunks(
    workspace: Path, db: MemoryDB, scope: Scope
) -> None:
    """With no embedder, embedded_hash can never be stamped — the anti-hash-gate
    branch must not misread that as "always pending" and re-upsert every call."""
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e0", "no embedder is wired here", "2026-08-01T00:00:00Z")
    surface = SurfaceIndex(db, workspace, scope, embedder=None)

    first = await surface.index_if_needed(embed=True)
    assert first == 1

    second = await surface.index_if_needed(embed=True)
    assert second == 0, "an unchanged chunk must not be treated as forever-pending"
