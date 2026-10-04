"""Memory recall over a large scope: never stall the loop, answer in well under 1 s.

Production crash (2026-10): a 978k-vector shared pool made ``vec_search`` score
every vector in pure Python ON the event loop; the 60 s systemd watchdog then
SIGABRTed ``arc ui``. These tests pin the two properties that prevent it:

* a heartbeat on the loop never sees a gap over 100 ms while recall runs —
  including the cold start, when the HNSW sidecar is missing and must be built;
* once warm, a full ``SurfaceIndex.search`` (vec + bm25 + graph + recency, fused)
  over 200k vectors finishes inside ``_WARM_SEARCH_BUDGET_S``.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path

import pytest
from packages.arcmemory.tests.conftest import bulk_load_vectors, low_rank_vectors

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.index import ann
from arcmemory.index.surface import SurfaceIndex
from arcmemory.types import Scope

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_DIMS = 384
_MAX_LOOP_GAP_S = 0.100
#: Warm, fused recall over 200k vectors. Measured ~10-30 ms on an M-series laptop;
#: the budget leaves room for a loaded CI box while still failing any full scan.
_WARM_SEARCH_BUDGET_S = 0.300


class _FixedEmbedder:
    """Returns one fixed query vector: the test controls exactly what is searched."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


async def _max_loop_gap(work: asyncio.Future[object]) -> float:
    """Largest gap between 10 ms heartbeats while ``work`` runs."""
    worst = 0.0
    last = time.perf_counter()
    while not work.done():
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        worst = max(worst, now - last - 0.01)
        last = now
    await work
    return worst


def _surface(db: MemoryDB, workspace: Path, scope: Scope, query: list[float]) -> SurfaceIndex:
    return SurfaceIndex(
        db,
        workspace,
        scope,
        embedder=_FixedEmbedder(query),
        seed_vocabulary=["alpha"],
    )


@pytest.fixture
def big_db(tmp_path: Path) -> MemoryDB:
    memdb = MemoryDB(tmp_path / "ws", dims=_DIMS)
    memdb.connect()
    yield memdb
    ann.forget_loaded_indexes()
    memdb.close()


async def test_recall_never_stalls_the_event_loop(big_db: MemoryDB, tmp_path: Path) -> None:
    scope = Scope(agent_did="did:arc:big", session_id="doc:pool")
    vectors = low_rank_vectors(100_000, _DIMS, seed=21)
    bulk_load_vectors(big_db, scope.key, vectors)
    surface = _surface(big_db, tmp_path / "ws", scope, vectors[123].tolist())

    # Cold: no sidecar yet, so this search also triggers the background build.
    cold = asyncio.ensure_future(surface.search("corpus alpha topic5", top_k=10))
    assert await _max_loop_gap(cold) < _MAX_LOOP_GAP_S
    assert cold.result().recalls

    build = asyncio.ensure_future(ann.scope_ann(big_db, scope.key).wait_built(timeout=300))
    assert await _max_loop_gap(build) < _MAX_LOOP_GAP_S
    assert build.result()

    warm = asyncio.ensure_future(surface.search("corpus alpha topic5", top_k=10))
    assert await _max_loop_gap(warm) < _MAX_LOOP_GAP_S
    assert "doc:123#0" in [recall.source for recall in warm.result().recalls]


async def test_warm_recall_over_200k_vectors_is_fast(big_db: MemoryDB, tmp_path: Path) -> None:
    scope = Scope(agent_did="did:arc:big", session_id="doc:pool")
    vectors = low_rank_vectors(200_000, _DIMS, seed=22)
    bulk_load_vectors(big_db, scope.key, vectors)
    surface = _surface(big_db, tmp_path / "ws", scope, vectors[777].tolist())
    await surface.search("warm up", top_k=10)
    assert await ann.scope_ann(big_db, scope.key).wait_built(timeout=300)

    timings = []
    for query in ("corpus alpha topic5 row777", "what does topic 12 say", "alpha"):
        start = time.perf_counter()
        result = await surface.search(query, top_k=10)
        timings.append(time.perf_counter() - start)
        assert result.recalls
    assert statistics.median(timings) < _WARM_SEARCH_BUDGET_S, timings
