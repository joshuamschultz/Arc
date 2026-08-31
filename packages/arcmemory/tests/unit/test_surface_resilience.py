"""Blast-radius containment — one failing chunk must never strand a whole pass.

The DGX incident (tsvector poison): a single oversized daily-log chunk made
``upsert_chunk`` raise; the surface pass aborted; every other chunk that same
pass lost its ``embedded_hash`` stamp, so the NEXT poll re-embedded the entire
corpus — forever. The size bound (``test_source_chunks``) stops that chunk from
existing; this test pins the second, independent guarantee: even if *some* chunk
still fails to upsert, the pass records every chunk that succeeded and never
raises, so a lone poison row can only ever re-cost itself, never the corpus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.index.backend import open_index_backend
from arcmemory.index.source import MAX_CHUNK_BYTES
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope

requires_vec = pytest.mark.skipif(
    not sqlite_vec_loadable(),
    reason="sqlite-vec extension not loadable in this Python/SQLite build",
)


def _append(episodic: EpisodicStore, scope: Scope, eid: str, text: str) -> None:
    episodic.append(Event(event_id=eid, ts="2026-08-01T00:00:00Z", scope=scope.key, kind="obs", text=text))


async def test_one_failing_upsert_does_not_abort_the_pass(
    workspace: Path, db: MemoryDB, scope: Scope, embedder: Any
) -> None:
    episodic = EpisodicStore(db, workspace)
    _append(episodic, scope, "e_ok1", "the first healthy chunk")
    _append(episodic, scope, "e_bad", "the poison chunk that fails to upsert")
    _append(episodic, scope, "e_ok2", "the second healthy chunk")

    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)

    real_upsert = surface._backend.upsert_chunk

    async def _flaky_upsert(*, chunk_id: str, **kwargs: Any) -> None:
        if chunk_id == "event:e_bad":
            raise RuntimeError("string is too long for tsvector")
        await real_upsert(chunk_id=chunk_id, **kwargs)

    surface._backend.upsert_chunk = _flaky_upsert  # type: ignore[method-assign]

    # Must not raise, even though one chunk's upsert blows up mid-pass.
    indexed = await surface.index_if_needed(embed=True)

    assert indexed == 2, "the two healthy chunks are indexed; the failing one is skipped"

    backend = open_index_backend("sqlite", db=db)
    embedded = await backend.embedded_hashes(scope.key)
    assert "event:e_ok1" in embedded and "event:e_ok2" in embedded, (
        "healthy chunks must be stamped so the next pass does NOT re-embed them"
    )
    assert "event:e_bad" not in embedded, "the failing chunk stays pending, and only it"


@requires_vec
async def test_oversized_daily_log_indexes_once_then_never_re_embeds(
    workspace: Path, db: MemoryDB, scope: Scope, embedder: Any
) -> None:
    """The exact incident, end to end: a >1 MB daily-log used to be one chunk that
    overflowed the backend on every pass, so the whole corpus re-embedded forever.
    Split into windows, it indexes cleanly ONCE and a second pass embeds nothing."""
    directory = workspace / "memory" / "daily-log"
    directory.mkdir(parents=True)
    # ~1.5 MB: over the Postgres tsvector limit that broke the real fleet, still
    # under the 2 MB OKF document ceiling — the exact band the DGX logs were in.
    entries = "\n\n".join(f"entry {i}: the meeting covered budget item {i}" for i in range(30000))
    (directory / "2026-07-17.md").write_text(f"---\ntype: daily-log\n---\n\n{entries}", "utf-8")
    assert len((directory / "2026-07-17.md").read_text("utf-8").encode("utf-8")) > MAX_CHUNK_BYTES

    surface = SurfaceIndex(db, workspace, scope, embedder=embedder)

    first = await surface.index_if_needed(embed=True)
    assert first > 1, "the oversized log fans out into several windows, all indexed"

    calls_after_first = embedder.calls
    second = await surface.index_if_needed(embed=True)
    assert second == 0, "an unchanged corpus must not re-embed — the poison loop is gone"
    assert embedder.calls == calls_after_first, "the second pass calls the embedder zero times"

    hits = await surface.search("budget item 5", top_k=3)
    assert hits.recalls, "the daily-log content is searchable, not lost"
