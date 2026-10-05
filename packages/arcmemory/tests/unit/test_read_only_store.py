"""A read-only store handle can search and list, and can never write (alpha-2 sync worker).

The main process reads connected-data stores while the sync worker writes them.
Its handle opens the SQLite file ``mode=ro`` with ``query_only``, never creates
or migrates a store that is not there yet, refuses every write entry point before
it touches anything, and builds a vector index in memory without saving a sidecar.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from packages.arcmemory.tests.conftest import bulk_load_vectors, low_rank_vectors

from arcmemory.connected_data import ConnectedDataService, ConnectedSource
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.index import ann

_DIMS = 64
_SCOPE = "did:arc:agent-a:doc:pool"


def test_a_read_only_handle_reads_what_the_writer_committed(tmp_path: Path) -> None:
    writer = MemoryDB(tmp_path / "ws")
    conn = writer.connect()
    conn.execute("INSERT INTO chunks (chunk_id, scope, source_path) VALUES ('c1', 's', 'p')")
    conn.commit()

    reader = MemoryDB(tmp_path / "ws", read_only=True)
    try:
        assert reader.connect().execute("SELECT chunk_id FROM chunks").fetchall() == [("c1",)]
        with pytest.raises(sqlite3.OperationalError):
            reader.connect().execute(
                "INSERT INTO chunks (chunk_id, scope, source_path) VALUES ('c2', 's', 'p')"
            )
    finally:
        reader.close()
        writer.close()


def test_a_read_only_handle_never_creates_a_store(tmp_path: Path) -> None:
    reader = MemoryDB(tmp_path / "absent", read_only=True)
    try:
        assert reader.connect().execute("SELECT COUNT(*) FROM chunks").fetchone() == (0,)
    finally:
        reader.close()
    assert not (tmp_path / "absent").exists(), "a reader created the store"


@pytest.mark.parametrize(
    "write",
    [
        lambda svc, src: svc.reset_source(src),
        lambda svc, src: svc.purge_source(src),
        lambda svc, src: svc.finish_sync(src),
        lambda svc, src: svc.relayout_source(src),
        lambda svc, src: svc.require_approved_mapping(src),
    ],
    ids=["reset", "purge", "finish", "relayout", "mapping-commit"],
)
async def test_every_write_through_a_read_only_service_is_refused(
    tmp_path: Path, write: object
) -> None:
    service = ConnectedDataService(
        tmp_path / "ws", "did:arc:agent-a", approval_store=None, read_only=True
    )
    source = ConnectedSource(
        connection_id="wiki", account_id="acct", source_kind="confluence", data_shape="document"
    )
    try:
        with pytest.raises(PermissionError, match="read-only"):
            await write(service, source)  # type: ignore[operator]  # reason: parametrized write
    finally:
        service.close()
    assert not (tmp_path / "ws").exists(), "a refused write touched the store"


@pytest.mark.skipif(not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here")
async def test_a_readers_vector_index_is_built_in_memory_and_never_saved(tmp_path: Path) -> None:
    writer = MemoryDB(tmp_path / "ws", dims=_DIMS)
    writer.connect()
    bulk_load_vectors(writer, _SCOPE, low_rank_vectors(500, _DIMS, seed=3))
    writer.close()
    ann.forget_loaded_indexes()

    reader = MemoryDB(tmp_path / "ws", dims=_DIMS, read_only=True)
    try:
        index = ann.scope_ann(reader, _SCOPE)
        assert await index.wait_built(timeout=60.0)
        assert index.mode == "hnsw"
        index.flush()
    finally:
        ann.forget_loaded_indexes()
        reader.close()
    assert not ann.sidecar_dir(reader).exists(), "a reader saved a vector sidecar"
