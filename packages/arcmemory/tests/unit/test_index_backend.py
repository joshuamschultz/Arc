"""COMP-007 — IndexBackend seam + pluggable-backend factory (T-1020/T-1021).

Parametrized conformance so a future deferred backend proves itself in one
place, mirroring ``arcstore.open_backend``. The sqlite implementation is
exercised for a real round-trip; ``postgres`` is a declared-but-deferred name
that must raise ``NotImplementedError`` (never a silent sqlite fallback), and an
unknown name is a plain configuration error (``ValueError``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.db import MemoryDB
from arcmemory.index.backend import IndexBackend, open_index_backend

_SCOPE = "did:arc:test-agent"


@pytest.fixture
def memdb(tmp_path: Path) -> MemoryDB:
    db = MemoryDB(tmp_path / "agent-workspace", dims=8)
    db.connect()
    return db


@pytest.mark.parametrize(
    "backend_name",
    [
        "sqlite",
        pytest.param(
            "postgres", marks=pytest.mark.xfail(raises=NotImplementedError, strict=True)
        ),
    ],
)
async def test_open_index_backend_conforms_to_protocol(
    memdb: MemoryDB, backend_name: str
) -> None:
    backend = open_index_backend(backend_name, db=memdb)
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


def test_open_index_backend_postgres_raises_not_implemented(memdb: MemoryDB) -> None:
    with pytest.raises(NotImplementedError):
        open_index_backend("postgres", db=memdb)


def test_open_index_backend_bogus_name_raises_value_error(memdb: MemoryDB) -> None:
    with pytest.raises(ValueError):
        open_index_backend("bogus", db=memdb)


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
