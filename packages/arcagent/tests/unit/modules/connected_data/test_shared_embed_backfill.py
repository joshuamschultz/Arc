"""Every store an agent writes or reads shared gets the doc-embed backfill.

A connection's shared store is written by whichever subscriber holds its sync
lease; chunks it wrote while the embedder could not serve stayed lexical-only
forever. Each agent's connected-data service drives a bounded backfill tick for
its own store and per shared store it subscribes to. A tick writes vectors, so
it runs in the sync worker, through a WRITER port (the subscriber's grant is
re-checked first). arcmemory keeps it to one backfill per store at a time.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncLimits
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data import service as service_module
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.modules.connected_data.shared import SharedKnowledge
from packages.arcagent.tests.sync_worker_fakes import approve_document_mapping, serve_from

_DID = "did:arc:test:agent"


class _Private:
    async def approved_mapping(self, source: SourceDescription) -> MappingPlan | None:
        del source
        return MappingPlan(
            mapping_id="approval-1",
            homes=(KnowledgeHome.DOCUMENT,),
            revision="r",
            content_hash="h",
        )

    async def documents_indexed(self, source: SourceDescription) -> int:
        del source
        return 0


class _Catalog:
    async def snapshot(self) -> tuple[Any, ...]:
        return ()

    def on_change(self, listener: Any) -> Any:
        del listener
        return lambda: None


class _WideEmbedder:
    """Deterministic 384-dim vectors (the default index width)."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([digest[i % 32] / 255.0 for i in range(384)])
        return out


def _source(connection_id: str = "wiki") -> SourceDescription:
    return SourceDescription(
        connection_id=connection_id, source_kind="confluence", account_id="acct"
    )


#: The on-device embedder the memory module defaults to; the worker builds it too.
_LOCAL_EMBED = ("local", "", "")


@pytest.fixture(autouse=True)
def _fleet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))


def _service(
    tmp_path: Path,
    embedder: Any,
    shared: Any = None,
    *,
    backend: FakeBackend | None = None,
    own_store: Any = None,
) -> ConnectedDataService:
    store = backend or FakeBackend()

    async def opener() -> FakeBackend:
        return store

    knowledge = shared or SharedKnowledge(
        agent_did=_DID,
        arcstore_opener=opener,
        embedder=lambda: embedder,
        profile=lambda: "lexical",
        embed=lambda: _LOCAL_EMBED,
    )
    service = ConnectedDataService(
        _Catalog(),  # type: ignore[arg-type]  # reason: snapshot is all these paths read
        agent_did=_DID,
        sync_store_opener=None,
        ingest_factory=None,
        limits=SyncLimits(),
        global_concurrency=1,
        shared=knowledge,
        own_store=own_store,
    )
    service._store = InMemorySourceSyncStore()
    return service


async def _write_during_outage(root: Path, principal: str, count: int) -> str:
    """Chunks in the shared store's doc pool with no vectors, as an outage leaves them."""
    from arcmemory.config import MemoryConfig
    from arcmemory.db import MemoryDB
    from arcmemory.doc_index import DocIndex, doc_scope
    from arcmemory.index.source import SourceChunk

    db = MemoryDB(root)
    try:
        await DocIndex(db, root, MemoryConfig(), embedder=None).index_source(
            "src",
            principal,
            [
                SourceChunk(
                    chunk_id=f"src:page{i}#0",
                    source_path=f"connected/src/page{i}.md",
                    text=f"page {i} on the release process",
                    classification="unclassified",
                    mtime=float(i),
                )
                for i in range(count)
            ],
        )
    finally:
        db.close()
    return doc_scope(principal, "src").key


def _pending(root: Path, scope: str) -> int:
    import sqlite3

    conn = sqlite3.connect(root / "memory" / "index.db")
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE scope=? AND "
            "(embedded_hash IS NULL OR embedded_hash <> content_hash)",
            (scope,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


@pytest.fixture(autouse=True)
def _forget_vector_indexes() -> Any:
    yield
    from arcmemory.index import ann

    ann.forget_loaded_indexes()


@pytest.mark.asyncio
async def test_a_subscribed_store_is_backfilled_in_the_sync_worker(tmp_path: Path) -> None:
    backend = FakeBackend()
    serve_from(backend, _DID)
    await approve_document_mapping(backend, _DID, approval_id="approval-1")
    service = _service(tmp_path, None, backend=backend)
    lane = await service._lane_for("wiki", _source(), _Private())  # type: ignore[arg-type]  # reason: approved_mapping/documents_indexed are all a lane decision reads
    assert lane is not None
    root = service._shared.root("wiki")  # type: ignore[union-attr]  # reason: built with a store
    scope = await _write_during_outage(root, lane.principal, 12)
    assert _pending(root, scope) == 12

    delay = await service.embed_backfill_once()

    assert _pending(root, scope) == 0, "the worker embedded nothing"
    assert 0 < delay <= service_module._EMBED_BACKFILL_IDLE_SECONDS


@pytest.mark.asyncio
async def test_no_shared_store_means_nothing_to_do(tmp_path: Path) -> None:
    service = _service(tmp_path, _WideEmbedder())
    assert await service.embed_backfill_once() == service_module._EMBED_BACKFILL_IDLE_SECONDS


class _FakePort:
    def __init__(self, delay: float | None = None, error: Exception | None = None) -> None:
        self.delay = delay
        self.error = error
        self.ticks = 0
        self.closed = False

    async def maintain_embeddings(self) -> float:
        self.ticks += 1
        if self.error is not None:
            raise self.error
        assert self.delay is not None
        return self.delay

    async def aclose(self) -> None:
        self.closed = True


class _FakeShared:
    """Hands out one fake writer port per connection; records who asked."""

    def __init__(self, ports: dict[str, _FakePort]) -> None:
        self.ports = ports
        self.writers: list[tuple[str, str]] = []

    async def writer(self, connection_id: str, approval_id: str) -> _FakePort:
        self.writers.append((connection_id, approval_id))
        return self.ports[connection_id]

    def close(self) -> None:
        return None


class _Lane:
    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id


@pytest.mark.asyncio
async def test_each_lane_ticks_once_and_the_soonest_delay_wins(tmp_path: Path) -> None:
    ports = {"a": _FakePort(delay=300.0), "b": _FakePort(delay=0.5)}
    shared = _FakeShared(ports)
    service = _service(tmp_path, None, shared=shared)
    service._lanes = {"a": _Lane("ap-a"), "b": _Lane("ap-b")}  # type: ignore[dict-item]  # reason: only approval_id is read

    assert await service.embed_backfill_once() == 0.5
    assert shared.writers == [("a", "ap-a"), ("b", "ap-b")]
    assert all(port.ticks == 1 and port.closed for port in ports.values())


@pytest.mark.asyncio
async def test_the_agents_own_store_ticks_beside_its_shared_ones(tmp_path: Path) -> None:
    own = _FakePort(delay=7.0)
    ports = {"a": _FakePort(delay=300.0)}

    async def open_own() -> _FakePort:
        return own

    service = _service(tmp_path, None, shared=_FakeShared(ports), own_store=open_own)
    service._lanes = {"a": _Lane("ap-a")}  # type: ignore[dict-item]  # reason: only approval_id is read

    assert await service.embed_backfill_once() == 7.0
    assert own.ticks == 1 and own.closed and ports["a"].ticks == 1


@pytest.mark.asyncio
async def test_a_failing_store_is_released_and_never_stops_the_others(tmp_path: Path) -> None:
    ports = {"a": _FakePort(error=RuntimeError("db locked")), "b": _FakePort(delay=0.5)}
    service = _service(tmp_path, None, shared=_FakeShared(ports))
    service._lanes = {"a": _Lane("ap-a"), "b": _Lane("ap-b")}  # type: ignore[dict-item]  # reason: only approval_id is read

    assert await service.embed_backfill_once() == 0.5
    assert ports["a"].closed and ports["b"].ticks == 1


@pytest.mark.asyncio
async def test_the_kill_switch_stops_shared_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_MEMORY_CONSOLIDATE_OFF", "1")
    ports = {"a": _FakePort(delay=0.5)}
    service = _service(tmp_path, None, shared=_FakeShared(ports))
    service._lanes = {"a": _Lane("ap-a")}  # type: ignore[dict-item]  # reason: only approval_id is read
    assert await service.embed_backfill_once() == service_module._EMBED_BACKFILL_IDLE_SECONDS
    assert ports["a"].ticks == 0


@pytest.mark.asyncio
async def test_the_backfill_loop_starts_with_the_service_and_stops_with_it(
    tmp_path: Path,
) -> None:
    ticked = asyncio.Event()
    ports = {"a": _FakePort(delay=3600.0)}
    service = _service(tmp_path, None, shared=_FakeShared(ports))
    service._lanes = {"a": _Lane("ap-a")}  # type: ignore[dict-item]  # reason: only approval_id is read
    original = service.embed_backfill_once

    async def observed() -> float:
        delay = await original()
        ticked.set()
        return delay

    service.embed_backfill_once = observed  # type: ignore[method-assign]  # reason: observe the loop's tick
    await service.start()
    await asyncio.wait_for(ticked.wait(), timeout=5.0)
    task = service._embed_backfill
    assert task is not None and not task.done()
    await service.close()
    assert task.done()
