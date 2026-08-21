"""COMP-007 — IndexBackend seam + pluggable-backend factory (T-1020/T-1021).

Parametrized conformance so each backend proves itself in one place, mirroring
``arcstore.open_backend``. The sqlite implementation is exercised for a real
round-trip; ``postgres`` (pgvector) is exercised only where ``ARC_MEMORY_PG_DSN``
names a live server (CI/deploy) and skipped otherwise. Postgres without a DSN is
a plain configuration error (``ValueError``), as is an unknown name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arcmemory.db import MemoryDB
from arcmemory.index.backend import IndexBackend, open_index_backend

_SCOPE = "did:arc:test-agent"
_PG_DSN = os.environ.get("ARC_MEMORY_PG_DSN")
_needs_pg = pytest.mark.skipif(
    not _PG_DSN, reason="requires a live Postgres server via ARC_MEMORY_PG_DSN"
)


@pytest.fixture
def memdb(tmp_path: Path) -> MemoryDB:
    db = MemoryDB(tmp_path / "agent-workspace", dims=8)
    db.connect()
    return db


@pytest.mark.parametrize(
    "backend_name",
    [
        "sqlite",
        pytest.param("postgres", marks=_needs_pg),
    ],
)
async def test_open_index_backend_conforms_to_protocol(
    memdb: MemoryDB, backend_name: str
) -> None:
    dsn = _PG_DSN if backend_name == "postgres" else None
    backend = open_index_backend(backend_name, db=memdb, dsn=dsn)
    assert isinstance(backend, IndexBackend)


async def test_sqlite_backend_round_trips_upsert_through_meta_and_text(
    memdb: MemoryDB,
) -> None:
    backend = open_index_backend("sqlite", db=memdb)

    await backend.upsert_chunk(
        scope=_SCOPE,
        chunk_id="event:e0",
        source_path="episodic",
        mtime=100.0,
        classification="unclassified",
        content_hash="abc123",
        text="hello world",
        embedding=None,
    )

    hashes = await backend.stored_hashes(_SCOPE)
    assert hashes == {"event:e0": "abc123"}

    meta = await backend.chunk_meta(_SCOPE, "event:e0")
    assert meta == ("episodic", "unclassified", 100.0)

    text = await backend.chunk_text(_SCOPE, "event:e0")
    assert text == "hello world"


def test_open_index_backend_postgres_requires_dsn(
    memdb: MemoryDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARC_MEMORY_PG_DSN", raising=False)
    with pytest.raises(ValueError, match="requires a DSN"):
        open_index_backend("postgres", db=memdb)


def test_open_index_backend_bogus_name_raises_value_error(memdb: MemoryDB) -> None:
    with pytest.raises(ValueError):
        open_index_backend("bogus", db=memdb)


@_needs_pg
async def test_postgres_backend_round_trips(memdb: MemoryDB) -> None:
    backend = open_index_backend("postgres", db=memdb, dsn=_PG_DSN)
    await backend.delete_scope(_SCOPE)
    await backend.upsert_chunk(
        scope=_SCOPE,
        chunk_id="event:pg0",
        source_path="episodic",
        mtime=100.0,
        classification="unclassified",
        content_hash="pgabc",
        text="hello postgres world",
        embedding=[0.1] * memdb.dims,
    )

    assert await backend.stored_hashes(_SCOPE) == {"event:pg0": "pgabc"}
    assert await backend.chunk_meta(_SCOPE, "event:pg0") == (
        "episodic",
        "unclassified",
        100.0,
    )
    assert await backend.chunk_text(_SCOPE, "event:pg0") == "hello postgres world"
    assert await backend.vec_search(_SCOPE, [0.1] * memdb.dims) == ["event:pg0"]
    await backend.delete_scope(_SCOPE)


# -- direct conformance for the methods SurfaceIndex now routes through -------


async def test_bm25_recency_chunk_texts_and_delete_scope_round_trip(memdb: MemoryDB) -> None:
    backend = open_index_backend("sqlite", db=memdb)
    for i in range(2):
        await backend.upsert_chunk(
            scope=_SCOPE, chunk_id=f"event:e{i}", source_path="episodic", mtime=float(i),
            classification="unclassified", content_hash=f"h{i}", text=f"launch note {i}",
            embedding=None,
        )
    # bm25 finds the keyword; recency lists newest first; chunk_texts returns bodies.
    assert set(await backend.bm25_search(_SCOPE, '"launch"')) == {"event:e0", "event:e1"}
    assert (await backend.recency_order(_SCOPE))[0] == "event:e1"
    assert dict(await backend.chunk_texts(_SCOPE)) == {
        "event:e0": "launch note 0",
        "event:e1": "launch note 1",
    }
    # delete_scope wipes the scope's chunks/fts.
    await backend.delete_scope(_SCOPE)
    assert await backend.stored_hashes(_SCOPE) == {}
    assert await backend.bm25_search(_SCOPE, '"launch"') == []


async def test_chunk_meta_and_text_return_none_for_a_missing_id(memdb: MemoryDB) -> None:
    backend = open_index_backend("sqlite", db=memdb)
    assert await backend.chunk_meta(_SCOPE, "nope") is None
    assert await backend.chunk_text(_SCOPE, "nope") is None
