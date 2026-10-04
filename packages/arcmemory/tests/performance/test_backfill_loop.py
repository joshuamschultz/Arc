"""The doc-pool embed backfill never stalls the event loop.

It runs inside the serving process next to live turns. Every SQLite read/write
and every HNSW sidecar update happens on worker threads, and the backfill yields
between batches, so a 10 ms heartbeat on the loop must never see a gap over
100 ms while 20k chunks (384-dim, the production width) are embedded and indexed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest
from packages.arcmemory.tests.conftest import low_rank_vectors

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.doc_index import doc_scope
from arcmemory.index import ann
from arcmemory.index.backend import open_index_backend
from arcmemory.index.backfill import DocEmbedBackfill

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_DIMS = 384
_CHUNKS = 20_000
_MAX_LOOP_GAP_S = 0.100
_AGENT = "did:arc:backfill-perf"


class _TableEmbedder:
    """Returns precomputed vectors keyed by the chunk's row number in its text."""

    def __init__(self, vectors: np.ndarray) -> None:
        self._vectors = vectors

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[int(text.split()[1])].tolist() for text in texts]


def _load_lexical_only(db: MemoryDB, scope: str, count: int) -> None:
    """Chunk + FTS rows with no vector, as an embedder outage leaves them."""
    conn = db.connect()
    for i in range(count):
        fts_rowid = conn.execute(
            "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
            (f"src:obj{i:06d}#0", scope, f"row {i} quarterly revenue topic{i % 89}"),
        ).lastrowid
        conn.execute(
            "INSERT INTO chunks (chunk_id, scope, source_path, mtime, classification, "
            "content_hash, fts_rowid) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"src:obj{i:06d}#0",
                scope,
                f"src/{i}.md",
                float(i),
                "unclassified",
                f"h{i}",
                fts_rowid,
            ),
        )
    conn.commit()


async def _max_loop_gap(work: asyncio.Future[object]) -> float:
    worst = 0.0
    last = time.perf_counter()
    while not work.done():
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        worst = max(worst, now - last - 0.01)
        last = now
    await work
    return worst


async def test_backfill_of_20k_chunks_never_blocks_the_loop(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path / "ws", dims=_DIMS)
    scope = doc_scope(_AGENT, "dropbox").key
    _load_lexical_only(db, scope, _CHUNKS)
    vectors = low_rank_vectors(_CHUNKS, _DIMS, seed=21)
    backfill = DocEmbedBackfill(
        db, MemoryConfig(), _TableEmbedder(vectors), scope_prefix=doc_scope(_AGENT, "").key
    )
    try:

        async def drain() -> int:
            total = 0
            while not (tick := await backfill.run_batches(8)).exhausted:
                total += tick.embedded
            return total + tick.embedded

        work = asyncio.ensure_future(drain())
        gap = await _max_loop_gap(work)
        assert work.result() == _CHUNKS
        assert gap < _MAX_LOOP_GAP_S, f"event loop stalled {gap * 1000:.0f} ms"

        backend = open_index_backend("sqlite", db=db)
        hits = await backend.vec_search(scope, vectors[123].tolist(), top_k=5)
        assert hits[0] == "src:obj000123#0"
    finally:
        ann.forget_loaded_indexes()
        db.close()
