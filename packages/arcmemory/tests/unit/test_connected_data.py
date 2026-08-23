from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ConnectedDataService,
    ConnectedObject,
    ConnectedObjectError,
    ConnectedObjectOrderError,
    ConnectedSource,
    SourceContent,
    SourceMappingPendingError,
)


def _source(*, account_id: str = "account") -> ConnectedSource:
    return ConnectedSource(
        connection_id="dropbox",
        account_id=account_id,
        source_kind="dropbox",
    )


def _service(workspace: Path, approval: ApprovalStore) -> ConnectedDataService:
    return ConnectedDataService(
        workspace,
        "did:arc:agent",
        approval_store=approval,
        config=MemoryConfig(doc_chunk_tokens=32),
    )


@pytest.mark.asyncio
async def test_pending_mapping_stages_once_and_writes_no_object_data(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval)

    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(_source())
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(_source())

    assert not (tmp_path / "memory" / "connected").exists()
    assert len(await approval.list()) == 1


def test_connected_object_state_is_not_added_to_memory_sqlite(tmp_path: Path) -> None:
    from arcmemory.db import MemoryDB

    conn = MemoryDB(tmp_path).connect()
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "connected_objects" not in tables


@pytest.mark.asyncio
async def test_approved_mapping_ingests_and_is_searchable(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval)
    source = _source()

    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)

    await service.ingest(
        source,
        ConnectedObject(
            object_id="report",
            locator="/reports/report.txt",
            version="1",
            media_type="text/plain",
            classification="unclassified",
        ),
        SourceContent(
            object_id="report",
            version="1",
            media_type="text/plain",
            content=b"quarterly revenue report",
        ),
        mapping,
    )

    hits = await service.document_search("revenue", source)
    assert hits and "quarterly revenue" in hits[0].text


@pytest.mark.asyncio
async def test_update_shrink_removes_old_chunks_and_delete_removes_object(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval)
    source = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)
    obj = ConnectedObject(
        object_id="report",
        locator="/reports/report.txt",
        version="2",
        media_type="text/plain",
        classification="unclassified",
        revision=1,
    )
    await service.ingest(
        source,
        obj,
        SourceContent(
            object_id="report",
            version="2",
            media_type="text/plain",
            content=b"old alpha\n\nold beta",
        ),
        mapping,
    )
    await service.ingest(
        source,
        obj.model_copy(update={"version": "3", "revision": 2}),
        SourceContent(
            object_id="report", version="3", media_type="text/plain", content=b"new only"
        ),
        mapping,
    )
    document_path = next((tmp_path / "memory" / "connected").rglob("*.md"))
    assert document_path.exists()
    hits = await service.document_search("old", source)
    assert not any("old alpha" in hit.text or "old beta" in hit.text for hit in hits)
    assert await service.document_search("new", source)

    await service.ingest(
        source,
        obj.model_copy(update={"version": "4", "deleted": True, "revision": 3}),
        None,
        mapping,
    )
    assert not any("new only" in hit.text for hit in await service.document_search("new", source))


@pytest.mark.asyncio
async def test_missing_classification_and_opaque_reordering_fail_closed(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval)
    source = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)
    object_v1 = ConnectedObject(
        object_id="report",
        locator="/reports/report.txt",
        version="opaque-1",
        media_type="text/plain",
        classification="unclassified",
        revision=1,
    )
    with pytest.raises(ConnectedObjectError, match="classification"):
        await service.ingest(
            source,
            object_v1.model_copy(update={"classification": ""}),
            SourceContent(object_id="report", version="opaque-1", content=b"secret"),
            mapping,
        )
    await service.ingest(
        source,
        object_v1,
        SourceContent(object_id="report", version="opaque-1", content=b"first"),
        mapping,
    )
    with pytest.raises(ConnectedObjectOrderError):
        await service.ingest(
            source,
            object_v1.model_copy(update={"version": "opaque-0"}),
            SourceContent(object_id="report", version="opaque-0", content=b"stale"),
            mapping,
        )
