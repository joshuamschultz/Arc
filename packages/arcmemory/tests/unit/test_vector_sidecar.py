"""HNSW sidecar for the SQLite vector channel (fast-vector-recall).

``vec0`` stays the source of truth. A per-scope usearch index beside
``index.db`` answers the top-k, is kept in step on every vector write/delete,
and heals itself (rebuild from ``vec0``) when it is missing, corrupt, or stale.
Search must never crash because the sidecar is bad, and must never return
another scope's ids (LLM08).
"""

from __future__ import annotations

import numpy as np
import pytest
from packages.arcmemory.tests.conftest import bulk_load_vectors, low_rank_vectors

from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.index import ann
from arcmemory.index.backend import ChunkWrite, open_index_backend

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_DIMS = 64
_SCOPE_A = "did:arc:agent-a:doc:pool"
_SCOPE_B = "did:arc:agent-b:doc:pool"


@pytest.fixture
def vdb(tmp_path) -> MemoryDB:
    memdb = MemoryDB(tmp_path / "ws", dims=_DIMS)
    memdb.connect()
    yield memdb
    ann.forget_loaded_indexes()
    memdb.close()


def _exact_topk(matrix: np.ndarray, query: np.ndarray, k: int) -> list[int]:
    sims = matrix @ (query / np.linalg.norm(query))
    return [int(i) for i in np.argsort(-sims)[:k]]


async def _built(db: MemoryDB, scope: str) -> ann.ScopeAnn:
    index = ann.scope_ann(db, scope)
    assert await index.wait_built(timeout=60.0), "sidecar never finished building"
    return index


async def test_hnsw_top10_matches_exact_cosine(vdb: MemoryDB) -> None:
    vectors = low_rank_vectors(20_000, _DIMS, seed=11)
    ids = bulk_load_vectors(vdb, _SCOPE_A, vectors)
    backend = open_index_backend("sqlite", db=vdb)
    await backend.vec_search(_SCOPE_A, vectors[0].tolist(), top_k=10)
    index = await _built(vdb, _SCOPE_A)
    assert index.mode == "hnsw"

    rng = np.random.default_rng(5)
    recalls = []
    for row in rng.integers(0, len(vectors), 50):
        query = vectors[row] + 0.05 * rng.standard_normal(_DIMS).astype(np.float32)
        got = await backend.vec_search(_SCOPE_A, query.tolist(), top_k=10)
        want = {ids[i] for i in _exact_topk(vectors, query, 10)}
        recalls.append(len(want & set(got)) / 10)
    assert float(np.mean(recalls)) >= 0.95


async def test_vec_search_is_bounded_by_top_k(vdb: MemoryDB) -> None:
    vectors = low_rank_vectors(1_000, _DIMS, seed=3)
    bulk_load_vectors(vdb, _SCOPE_A, vectors)
    backend = open_index_backend("sqlite", db=vdb)
    assert len(await backend.vec_search(_SCOPE_A, vectors[0].tolist(), top_k=7)) == 7
    assert len(await backend.vec_search(_SCOPE_A, vectors[0].tolist())) == 200


async def test_upsert_and_delete_keep_the_sidecar_in_sync(vdb: MemoryDB) -> None:
    backend = open_index_backend("sqlite", db=vdb)
    vectors = low_rank_vectors(300, _DIMS, seed=9)
    await backend.upsert_chunks(
        _SCOPE_A,
        [
            ChunkWrite(f"obj{i}#0", "p", float(i), "unclassified", f"h{i}", f"t{i}", v.tolist())
            for i, v in enumerate(vectors)
        ],
    )
    await _built(vdb, _SCOPE_A)
    assert (await backend.vec_search(_SCOPE_A, vectors[42].tolist(), top_k=1)) == ["obj42#0"]

    # Re-embedding a chunk moves it: the old vector must stop answering.
    await backend.upsert_chunk(
        scope=_SCOPE_A,
        chunk_id="obj42#0",
        source_path="p",
        mtime=1.0,
        classification="unclassified",
        content_hash="h42b",
        text="t42",
        embedding=vectors[7].tolist(),
    )
    assert (await backend.vec_search(_SCOPE_A, vectors[42].tolist(), top_k=1)) != ["obj42#0"]

    await backend.delete_object(_SCOPE_A, "obj7")
    await backend.delete_object(_SCOPE_A, "obj42")
    hits = await backend.vec_search(_SCOPE_A, vectors[7].tolist(), top_k=50)
    assert "obj7#0" not in hits and "obj42#0" not in hits

    # Persisted + reloaded (a process restart) without a rebuild.
    ann.scope_ann(vdb, _SCOPE_A).flush()
    ann.forget_loaded_indexes()
    assert (await backend.vec_search(_SCOPE_A, vectors[9].tolist(), top_k=1)) == ["obj9#0"]
    reloaded = await _built(vdb, _SCOPE_A)
    assert reloaded.source == "sidecar"
    assert "obj7#0" not in await backend.vec_search(_SCOPE_A, vectors[7].tolist(), top_k=50)

    await backend.delete_scope(_SCOPE_A)
    assert await backend.vec_search(_SCOPE_A, vectors[9].tolist()) == []


async def test_missing_sidecar_rebuilds_from_vec0(vdb: MemoryDB) -> None:
    vectors = low_rank_vectors(500, _DIMS, seed=1)
    ids = bulk_load_vectors(vdb, _SCOPE_A, vectors)
    backend = open_index_backend("sqlite", db=vdb)
    index_path, _ = ann.sidecar_paths(vdb, _SCOPE_A)
    assert not index_path.exists()

    assert (await backend.vec_search(_SCOPE_A, vectors[3].tolist(), top_k=1)) == [ids[3]]
    index = await _built(vdb, _SCOPE_A)
    assert index.source == "rebuild"
    assert index_path.exists()


async def test_corrupt_sidecar_never_crashes_and_heals(vdb: MemoryDB) -> None:
    vectors = low_rank_vectors(500, _DIMS, seed=2)
    ids = bulk_load_vectors(vdb, _SCOPE_A, vectors)
    backend = open_index_backend("sqlite", db=vdb)
    await backend.vec_search(_SCOPE_A, vectors[0].tolist(), top_k=1)
    await _built(vdb, _SCOPE_A)
    ann.scope_ann(vdb, _SCOPE_A).flush()
    index_path, _ = ann.sidecar_paths(vdb, _SCOPE_A)
    index_path.write_bytes(b"\x00garbage" * 64)
    ann.forget_loaded_indexes()

    assert (await backend.vec_search(_SCOPE_A, vectors[8].tolist(), top_k=1)) == [ids[8]]
    healed = await _built(vdb, _SCOPE_A)
    assert healed.source == "rebuild"


async def test_out_of_band_vec0_write_is_detected_as_stale(vdb: MemoryDB) -> None:
    vectors = low_rank_vectors(400, _DIMS, seed=4)
    bulk_load_vectors(vdb, _SCOPE_A, vectors[:200])
    backend = open_index_backend("sqlite", db=vdb)
    await backend.vec_search(_SCOPE_A, vectors[0].tolist(), top_k=1)
    first = await _built(vdb, _SCOPE_A)
    ann.forget_loaded_indexes()
    del first

    # Another writer (a rebuild, another process) adds vectors and stamps the scope.
    late = bulk_load_vectors(vdb, _SCOPE_A, vectors[200:], start=200)
    ann.mark_scope_written(vdb.connect(), _SCOPE_A)
    vdb.connect().commit()

    assert (await backend.vec_search(_SCOPE_A, vectors[250].tolist(), top_k=1)) == [late[50]]


async def test_scope_a_search_never_returns_scope_b_ids(vdb: MemoryDB) -> None:
    vectors = low_rank_vectors(300, _DIMS, seed=6)
    a_ids = bulk_load_vectors(vdb, _SCOPE_A, vectors, prefix="a")
    b_ids = bulk_load_vectors(vdb, _SCOPE_B, vectors, prefix="b")  # identical vectors
    backend = open_index_backend("sqlite", db=vdb)
    for row in (0, 17, 299):
        hits = await backend.vec_search(_SCOPE_A, vectors[row].tolist(), top_k=300)
        assert hits and set(hits) <= set(a_ids)
    await _built(vdb, _SCOPE_A)
    await _built(vdb, _SCOPE_B)
    hits = await backend.vec_search(_SCOPE_A, vectors[5].tolist(), top_k=300)
    assert hits and not set(hits) & set(b_ids)
    assert ann.sidecar_paths(vdb, _SCOPE_A) != ann.sidecar_paths(vdb, _SCOPE_B)


async def test_chunk_moved_to_another_scope_leaves_the_old_scope(vdb: MemoryDB) -> None:
    backend = open_index_backend("sqlite", db=vdb)
    vectors = low_rank_vectors(50, _DIMS, seed=8)
    await backend.upsert_chunks(
        _SCOPE_A,
        [
            ChunkWrite(f"x{i}#0", "p", 1.0, "unclassified", f"h{i}", f"t{i}", v.tolist())
            for i, v in enumerate(vectors)
        ],
    )
    await _built(vdb, _SCOPE_A)
    await backend.upsert_chunk(
        scope=_SCOPE_B,
        chunk_id="x3#0",
        source_path="p",
        mtime=1.0,
        classification="unclassified",
        content_hash="h3",
        text="t3",
        embedding=None,
    )
    assert "x3#0" not in await backend.vec_search(_SCOPE_A, vectors[3].tolist(), top_k=50)
    assert await backend.vec_search(_SCOPE_B, vectors[3].tolist(), top_k=5) == ["x3#0"]


async def test_bm25_and_recency_are_bounded(vdb: MemoryDB) -> None:
    bulk_load_vectors(vdb, _SCOPE_A, low_rank_vectors(600, _DIMS, seed=10))
    backend = open_index_backend("sqlite", db=vdb)
    assert len(await backend.bm25_search(_SCOPE_A, '"corpus"', top_k=25)) == 25
    assert len(await backend.bm25_search(_SCOPE_A, '"corpus"')) == 200
    recent = await backend.recency_order(_SCOPE_A, limit=10)
    assert recent == [f"doc:{i}#0" for i in range(599, 589, -1)]
    assert len(await backend.recency_order(_SCOPE_A, limit=None)) == 600


async def test_chunk_texts_for_terms_is_bounded(vdb: MemoryDB) -> None:
    bulk_load_vectors(vdb, _SCOPE_A, low_rank_vectors(600, _DIMS, seed=12))
    backend = open_index_backend("sqlite", db=vdb)
    rows = await backend.chunk_texts(_SCOPE_A, terms=["topic5"], limit=100)
    assert rows and all("topic5" in text for _, text in rows)
    assert len(await backend.chunk_texts(_SCOPE_A, terms=["corpus"], limit=40)) == 40
