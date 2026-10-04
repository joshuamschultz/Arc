"""An agent's document pools live in exactly one store, whatever its memory backend.

Production (2026-10-04): the fleet runs the Brain's memory index on Postgres
(``index_backend = "postgres"`` via dynamics), while connected-data ports wrote
every doc pool into the workspace SQLite ``index.db``. The Brain then searched
(and backfilled) doc pools in Postgres, where there were none: the agent's own
connected documents were unsearchable and 1.2M chunks never got vectors.

Now one resolver (``doc_pool_backend``) names the doc-pool store, and every doc
path uses it: connected-data writes, the Brain's ``ingest_batch`` document home,
``document_search`` and the embed backfill. The Brain's own (non-doc) memory
scope stays on its configured backend.

The Brain here is configured for a Postgres server that does not answer: any doc
path that still goes to the Brain's backend fails loudly instead of passing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.doc_index import DocIndex, doc_scope
from arcmemory.index import ann
from arcmemory.index.backend import (
    PostgresIndexBackend,
    SqliteIndexBackend,
    backend_for_scope,
    doc_pool_backend,
)
from arcmemory.index.backfill import read_embed_backlog
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.source import SourceChunk
from arcmemory.mapping import commit_mapping
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import SourceMapping, SourceRecord

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_DID = "did:arc:doc-pool-agent"
#: A Postgres DSN nobody listens on: touching it is a connection error.
_DEAD_DSN = "postgresql://arc:arc@127.0.0.1:1/arc_memory"


class _WideEmbedder:
    """Deterministic 384-dim vectors (the default index width)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [
            [hashlib.sha256(text.encode()).digest()[i % 32] / 255.0 for i in range(384)]
            for text in texts
        ]


@pytest.fixture
def postgres_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARC_MEMORY_PG_DSN", _DEAD_DSN)
    brain = ArcMemoryBrain(
        tmp_path / "ws",
        _DID,
        config=MemoryConfig(index_backend="postgres"),
        embedder=_WideEmbedder(),
    )
    yield brain
    ann.forget_loaded_indexes()


def _connected_chunks() -> list[SourceChunk]:
    return [
        SourceChunk(
            chunk_id=f"dropbox:inv{i}#0",
            source_path=f"connected/dropbox/invoice-{i}.md",
            text=f"invoice {i} from northwind for consulting hours",
            classification="unclassified",
            mtime=float(i),
        )
        for i in range(6)
    ]


async def _connected_data_writes(workspace: Path) -> None:
    """What a connected-data port does: its own MemoryDB, arcmemory's default config."""
    db = MemoryDB(workspace)
    try:
        await DocIndex(db, workspace, MemoryConfig(), embedder=None).index_source(
            "dropbox", _DID, _connected_chunks()
        )
    finally:
        db.close()


def _pools(workspace: Path) -> dict[str, int]:
    backlog = read_embed_backlog(workspace / "memory" / "index.db")
    return {scope: pool.total for scope, pool in backlog.items()}


async def test_connected_data_pools_are_found_by_a_postgres_brains_document_search(
    postgres_brain: ArcMemoryBrain, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    await _connected_data_writes(workspace)

    hits = await postgres_brain.document_search("northwind invoice consulting")

    assert hits, "the agent's own connected documents must be searchable"
    assert {hit.source_id for hit in hits} == {"dropbox"}


async def test_pushed_document_records_land_in_and_are_found_from_the_same_store(
    postgres_brain: ArcMemoryBrain, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    await _connected_data_writes(workspace)
    store = SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), _DID)
    commit_mapping(SourceMapping(source_id="crm", homes=["document"]), store=store)

    await postgres_brain.ingest_batch(
        "crm",
        [SourceRecord(external_id="acct-9", text="contoso renewal quote for fiscal year")],
    )

    pools = _pools(workspace)
    assert doc_scope(_DID, "crm").key in pools  # the same SQLite file as connected data
    assert doc_scope(_DID, "dropbox").key in pools
    hits = await postgres_brain.document_search("contoso renewal quote")
    assert "crm" in {hit.source_id for hit in hits}


async def test_the_brains_backfill_drains_the_doc_pool_store(
    postgres_brain: ArcMemoryBrain, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    await _connected_data_writes(workspace)
    assert (
        read_embed_backlog(workspace / "memory" / "index.db")[
            doc_scope(_DID, "dropbox").key
        ].pending
        == 6
    )

    await postgres_brain.maintain_doc_embeddings()

    assert all(
        pool.pending == 0
        for pool in read_embed_backlog(workspace / "memory" / "index.db").values()
    )


def test_only_doc_pools_move_the_memory_scope_keeps_its_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_MEMORY_PG_DSN", _DEAD_DSN)
    db = MemoryDB(tmp_path / "ws")
    config = MemoryConfig(index_backend="postgres")
    try:
        assert isinstance(backend_for_scope(_DID, config, db), PostgresIndexBackend)
        assert isinstance(backend_for_scope(f"{_DID}:session-1", config, db), PostgresIndexBackend)
        doc = backend_for_scope(doc_scope(_DID, "dropbox").key, config, db)
        assert isinstance(doc, SqliteIndexBackend)
        assert isinstance(doc_pool_backend(db), SqliteIndexBackend)
    finally:
        db.close()
