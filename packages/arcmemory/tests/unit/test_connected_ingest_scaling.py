"""One connected object costs one object, whatever the source already holds.

Every ingested object used to re-walk the source's whole inventory, rewrite its
``index.md`` (26 MB on a live box), re-read it, and embed + upsert the whole
file as one chunk. The ingest was quadratic and all of it ran on the event
loop. The routing index is now brought up to date once per sync run by
``finish_sync``; per-object work touches only that object.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from arcokf import validate_collection_index
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

import arcmemory.collection_index as collection_index
import arcmemory.connected_data as connected_data
from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ApprovedMapping,
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingPendingError,
)
from arcmemory.doc_index import doc_scope
from arcmemory.index.backend import open_index_backend
from arcmemory.index.source import MAX_CHUNK_BYTES

_DID = "did:arc:scaling"


def _source() -> ConnectedSource:
    return ConnectedSource(
        connection_id="drive",
        account_id="account",
        source_kind="drive",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


async def _granted(workspace: Path) -> tuple[ConnectedDataService, ApprovedMapping]:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(workspace, _DID, approval_store=approval, config=MemoryConfig())
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(_source())
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    return service, await service.require_approved_mapping(_source())


async def _ingest(
    service: ConnectedDataService, mapping: ApprovedMapping, number: int, text: str = ""
) -> None:
    object_id = f"doc-{number:05d}"
    body = text or f"Heading {number}\n\nquarterly figures for team {number}"
    await service.ingest(
        _source(),
        ConnectedObject(
            object_id=object_id,
            locator=f"/docs/{object_id}.txt",
            version="1",
            media_type="text/plain",
            classification="unclassified",
            revision=1,
        ),
        SourceContent(
            object_id=object_id, version="1", media_type="text/plain", content=body.encode()
        ),
        mapping,
    )


@contextmanager
def _vm_steps(conn: sqlite3.Connection) -> Iterator[list[int]]:
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(tick, 100)
    try:
        yield steps
    finally:
        conn.set_progress_handler(None, 0)


async def test_per_object_ingest_never_walks_or_rewrites_the_collection_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, mapping = await _granted(tmp_path)
    walks: list[str] = []

    def forbid(name: str) -> Any:
        def record(*args: object, **kwargs: object) -> Any:
            walks.append(name)
            raise AssertionError(f"{name} ran inside a per-object ingest")

        return record

    with monkeypatch.context() as patch:
        patch.setattr(collection_index, "inventory_documents", forbid("inventory_documents"))
        patch.setattr(collection_index, "candidate_documents", forbid("candidate_documents"))
        patch.setattr(collection_index, "render_collection_index", forbid("render"))
        for number in range(30):
            await _ingest(service, mapping, number)

    assert walks == []
    root = tmp_path / "memory" / "connected" / mapping.source_id
    assert not (root / "index.md").exists()

    await service.finish_sync(_source())

    validation = validate_collection_index(root / "index.md", root)
    assert validation.valid, validation.error
    assert len(validation.entries) == 30
    hits = await service.document_search("Heading", _source())
    assert any(hit.chunk_id == f"index:{mapping.source_id}" for hit in hits)


async def test_per_object_database_work_does_not_grow_with_the_inventory(
    tmp_path: Path,
) -> None:
    service, mapping = await _granted(tmp_path)
    conn = service._db.connect()
    with _vm_steps(conn) as first:
        await _ingest(service, mapping, 0)
    for number in range(1, 400):
        await _ingest(service, mapping, number)
    await service.finish_sync(_source())
    with _vm_steps(conn) as later:
        await _ingest(service, mapping, 400)

    assert later[0] <= first[0] * 2 + 50, (first[0], later[0])


async def test_ingest_extracts_and_writes_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow parser must not freeze chat, NATS and health while a source syncs."""
    service, mapping = await _granted(tmp_path)

    class SlowExtractor:
        def extract(self, content: bytes, *, filename: str = "") -> str:
            time.sleep(0.15)
            return content.decode()

    monkeypatch.setattr(connected_data, "get_extractor", lambda *a, **k: SlowExtractor())
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    for number in range(4):
        await _ingest(service, mapping, number)
    stop.set()
    await task

    assert max(gaps) < 0.1, max(gaps)


async def test_a_large_routing_index_is_indexed_in_bounded_windows(tmp_path: Path) -> None:
    service, mapping = await _granted(tmp_path)
    for number in range(700):
        await _ingest(service, mapping, number, text=f"{'w' * 120} {number}")
    await service.finish_sync(_source())

    backend = open_index_backend("sqlite", db=service._db)
    scope = doc_scope(_DID, mapping.source_id).key
    windows = [f"index:{mapping.source_id}", f"index:{mapping.source_id}#1"]
    texts = [await backend.chunk_text(scope, chunk_id) for chunk_id in windows]
    assert all(text is not None for text in texts)
    assert all(len((text or "").encode()) <= MAX_CHUNK_BYTES for text in texts)


async def test_deleting_an_object_updates_the_index_at_finish(tmp_path: Path) -> None:
    service, mapping = await _granted(tmp_path)
    for number in range(2):
        await _ingest(service, mapping, number)
    await service.finish_sync(_source())
    await service.ingest(
        _source(),
        ConnectedObject(
            object_id="doc-00000",
            locator="/docs/doc-00000.txt",
            version="2",
            deleted=True,
            classification="unclassified",
            revision=2,
        ),
        None,
        mapping,
    )
    await service.finish_sync(_source())

    root = tmp_path / "memory" / "connected" / mapping.source_id
    validation = validate_collection_index(root / "index.md", root)
    assert validation.valid and len(validation.entries) == 1
