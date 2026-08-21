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
