"""The agent's own connected-document pools are backfilled where they are written.

Production (2026-10-04): the fleet runs the Brain on ``index_backend = "postgres"``,
but connected-data ports build arcmemory's ``ConnectedDataService`` with no config
override, so every doc pool lives in the workspace SQLite ``index.db`` (josh_agent:
1.24M doc chunks, 1.2M without a vector). The backfill was driven from the Brain,
with the Brain's Postgres config: it found no doc pools there and never touched
SQLite. The own-store backfill now runs through the SAME port the connected-data
sync writes with, so "where doc pools live" has one answer.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import SyncLimits
from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data import service as service_module
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter
from arcagent.modules.connected_data.service import ConnectedDataService

_DID = "did:arc:test:own-store"


class _Catalog:
    async def snapshot(self) -> tuple[Any, ...]:
        return ()

    def on_change(self, listener: Any) -> Any:
        del listener
        return lambda: None


class _WideEmbedder:
    """Deterministic 384-dim vectors (the default index width)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [
            [hashlib.sha256(text.encode()).digest()[i % 32] / 255.0 for i in range(384)]
            for text in texts
        ]


def _service(own_store_opener: Any) -> ConnectedDataService:
    service = ConnectedDataService(
        _Catalog(),  # type: ignore[arg-type]  # reason: snapshot/on_change are all these paths read
        agent_did=_DID,
        sync_store_opener=None,
        ingest_factory=None,
        limits=SyncLimits(),
        global_concurrency=1,
        own_store_opener=own_store_opener,
    )
    service._store = InMemorySourceSyncStore()
    return service


async def _seed_sqlite_doc_pool(workspace: Path, count: int) -> str:
    """Doc chunks as the connected-data sync writes them during an embedder outage."""
    from arcmemory.config import MemoryConfig
    from arcmemory.db import MemoryDB
    from arcmemory.doc_index import DocIndex, doc_scope
    from arcmemory.index.source import SourceChunk

    db = MemoryDB(workspace)
    try:
        await DocIndex(db, workspace, MemoryConfig(), embedder=None).index_source(
            "dropbox",
            _DID,
            [
                SourceChunk(
                    chunk_id=f"dropbox:f{i}#0",
                    source_path=f"connected/dropbox/{i}.md",
                    text=f"file {i} about invoices",
                    classification="unclassified",
                    mtime=float(i),
                )
                for i in range(count)
            ],
        )
    finally:
        db.close()
    return doc_scope(_DID, "dropbox").key


def _pending(workspace: Path) -> int:
    from arcmemory.index.backfill import read_embed_backlog

    return sum(b.pending for b in read_embed_backlog(workspace / "memory" / "index.db").values())


@pytest.fixture(autouse=True)
def _forget_vector_indexes() -> Any:
    yield
    from arcmemory.index import ann

    ann.forget_loaded_indexes()


@pytest.mark.asyncio
async def test_the_own_store_drains_through_the_connected_data_port(tmp_path: Path) -> None:
    """The Brain's backend never enters into it: the writer's port is the store."""
    workspace = tmp_path / "workspace"
    await _seed_sqlite_doc_pool(workspace, 25)
    assert _pending(workspace) == 25

    async def open_own_store() -> ArcMemoryIngestAdapter:
        return ArcMemoryIngestAdapter(
            workspace, _DID, approval_store=None, embedder=_WideEmbedder()
        )

    service = _service(open_own_store)
    delay = await service.embed_backfill_once()

    assert _pending(workspace) == 0
    assert 0 < delay <= service_module._EMBED_BACKFILL_IDLE_SECONDS


class _FakePort:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.ticks = 0
        self.closed = False

    async def maintain_embeddings(self) -> float:
        self.ticks += 1
        return self.delay

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_the_own_store_ticks_and_is_released_without_any_shared_store() -> None:
    port = _FakePort(delay=0.5)

    async def open_own_store() -> _FakePort:
        return port

    service = _service(open_own_store)
    assert await service.embed_backfill_once() == 0.5
    assert port.ticks == 1 and port.closed


@pytest.mark.asyncio
async def test_the_loop_runs_for_an_own_store_alone() -> None:
    async def open_own_store() -> _FakePort:
        return _FakePort(delay=3600.0)

    service = _service(open_own_store)
    await service.start()
    try:
        assert service._embed_backfill is not None
    finally:
        await service.close()


def test_the_runtime_opens_the_own_store_like_the_sync_port(tmp_path: Path) -> None:
    """Same class, same (absent) config override as the sync's ingest port."""
    _runtime.configure(
        workspace=tmp_path,
        agent_did=_DID,
        arcstore_opener=_opener(FakeBackend()),
        source_catalog=_Catalog(),
    )
    state = _runtime.state()
    assert state.own_store_opener is not None


@pytest.mark.asyncio
async def test_the_own_store_port_carries_no_memory_config_override(tmp_path: Path) -> None:
    _runtime.configure(
        workspace=tmp_path,
        agent_did=_DID,
        arcstore_opener=_opener(FakeBackend()),
        source_catalog=_Catalog(),
    )
    opener = _runtime.state().own_store_opener
    assert opener is not None
    port = await opener()
    try:
        assert isinstance(port, ArcMemoryIngestAdapter)
        assert port._config is None  # doc pools: arcmemory's default store, as the sync writes
        assert port._workspace == tmp_path
    finally:
        await port.aclose()


def _opener(backend: FakeBackend) -> Any:
    async def opener() -> FakeBackend:
        return backend

    return opener
