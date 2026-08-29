from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    ConnectedSourceShape,
    SourceContent,
    SourceMappingDeniedError,
    SourceMappingPendingError,
)


def _source(*, account_id: str = "account") -> ConnectedSource:
    return ConnectedSource(
        connection_id="dropbox",
        account_id=account_id,
        source_kind="dropbox",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


def _service(
    workspace: Path, approval: ApprovalStore, *, tier: str = "personal"
) -> ConnectedDataService:
    return ConnectedDataService(
        workspace,
        "did:arc:agent",
        approval_store=approval,
        config=MemoryConfig(doc_chunk_tokens=32, tier=tier),
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
async def test_approved_mapping_is_durable_past_its_staging_expiry(tmp_path: Path) -> None:
    """A granted mapping must not lapse on the 24h staging timer.

    ``expires_at`` bounds only the *pending* window — an unacted request
    auto-cancels. Once an operator approves, the mapping is durable until the
    mapping structure itself changes (a change re-derives a new ``call_hash``
    and re-triggers approval). Without this, every connected source reverts to
    ``awaiting_mapping`` a day after approval and syncs nothing — the exact
    "0 bytes / awaiting mapping" regression seen across live connectors.
    """
    backend = FakeBackend()
    approval = ApprovalStore(backend)
    service = _service(tmp_path, approval)
    source = _source()

    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")

    # Age the approved row an hour past its staging expiry — a day has passed.
    aged = await approval.get(pending.id)
    assert aged is not None
    await backend.mutable_write(
        "approvals",
        pending.id,
        aged.model_copy(
            update={"expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
        ).model_dump(mode="json"),
        actor_did="did:operator",
    )

    # It resolves without re-staging, and the aged approval still gates ingest.
    mapping = await service.require_approved_mapping(source)
    assert mapping.mapping_id == pending.id
    assert len(await approval.list()) == 1
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
            content=b"durable revenue report",
        ),
        mapping,
    )
    hits = await service.document_search("revenue", source)
    assert hits and "durable revenue" in hits[0].text


@pytest.mark.asyncio
async def test_purge_source_removes_retrievable_documents_and_mapping(tmp_path: Path) -> None:
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
            object_id="confidential-report",
            locator="/reports/confidential.txt",
            version="1",
            media_type="text/plain",
            classification="unclassified",
        ),
        SourceContent(
            object_id="confidential-report",
            version="1",
            media_type="text/plain",
            content=b"revoke this searchable source content",
        ),
        mapping,
    )
    assert await service.document_search("searchable source", source)

    await service.purge_source(source)

    assert await service.document_search("searchable source", source) == []
    assert not (tmp_path / "memory" / "connected" / mapping.source_id).exists()


class _GenerationState:
    def __init__(self) -> None:
        self.generation = 1

    async def get_object_state(self, source_id: str, object_id: str):  # type: ignore[no-untyped-def]
        del source_id, object_id
        return None

    async def put_object_state(self, source_id: str, object_id: str, state):  # type: ignore[no-untyped-def]
        del source_id, object_id, state

    async def list_object_ids(self, source_id: str) -> list[str]:
        del source_id
        return []

    async def clear_source(self, source_id: str) -> None:
        del source_id

    async def source_generation(self, connection_id: str) -> int:
        del connection_id
        return self.generation


@pytest.mark.asyncio
async def test_reconnect_requires_a_new_mapping_approval_after_generation_fence(
    tmp_path: Path,
) -> None:
    approval = ApprovalStore(FakeBackend())
    generation = _GenerationState()
    service = ConnectedDataService(
        tmp_path,
        "did:arc:agent",
        approval_store=approval,
        object_state=generation,
        config=MemoryConfig(doc_chunk_tokens=32),
    )
    original = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(original)
    old_pending = (await approval.list())[0]
    await approval.resolve(old_pending.id, status="approved", actor_did="did:operator")
    assert (await service.require_approved_mapping(original)).mapping_id == old_pending.id

    await service.purge_source(original)
    generation.generation = 2
    with pytest.raises(SourceMappingDeniedError):
        await service.require_approved_mapping(original)
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(original.model_copy(update={"generation": 2}))

    pending = await approval.list()
    assert len(pending) == 2
    assert pending[0].id != old_pending.id


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
async def test_complete_snapshot_removes_missing_object_and_allows_restore(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval)
    source = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)
    obj = ConnectedObject(
        object_id="gone",
        locator="/gone.txt",
        version="1",
        media_type="text/plain",
        classification="unclassified",
        revision=1,
    )
    content = SourceContent(
        object_id="gone", version="1", media_type="text/plain", content=b"vanishing text"
    )
    await service.ingest(source, obj, content, mapping)
    assert await service.document_search("vanishing", source)

    await service.complete_snapshot(source, frozenset(), mapping)
    assert not await service.document_search("vanishing", source)

    await service.ingest(source, obj, content, mapping)
    assert await service.document_search("vanishing", source)


@pytest.mark.asyncio
async def test_missing_classification_and_opaque_reordering_fail_closed(tmp_path: Path) -> None:
    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval, tier="federal")
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


@pytest.mark.asyncio
async def test_an_unlabelled_object_ingests_below_federal(tmp_path: Path) -> None:
    """No connector labels its objects, so strict everywhere meant sync never ran.

    Federal fails closed on a missing label (the test above). Personal and
    enterprise read it as UNCLASSIFIED, the same as every other classification
    read in this service — otherwise the first object of every connected account
    is refused and the source is permanently `failed`.
    """
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
            version="opaque-1",
            media_type="text/plain",
            classification="",
            revision=1,
        ),
        SourceContent(object_id="report", version="opaque-1", content=b"hello"),
        mapping,
    )

    assert await service.document_search("hello", source)


@pytest.mark.asyncio
async def test_a_document_parser_crash_skips_one_object_not_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser raises whatever it likes over a file someone else produced.

    One malformed PDF used to end the whole sync, so an account was indexed as
    far as its first bad file and no further.
    """
    import arcmemory.extract as extract_module

    class _Exploding:
        mimes = ("text/plain",)

        def extract(self, data: bytes, *, filename: str = "") -> str:
            raise LookupError("parser limit reached")

    monkeypatch.setattr(extract_module, "get_extractor", lambda *a, **k: _Exploding())
    monkeypatch.setattr("arcmemory.connected_data.get_extractor", lambda *a, **k: _Exploding())

    approval = ApprovalStore(FakeBackend())
    service = _service(tmp_path, approval)
    source = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)

    with pytest.raises(ConnectedObjectError, match="extraction failed"):
        await service.ingest(
            source,
            ConnectedObject(
                object_id="report",
                locator="/reports/report.txt",
                version="opaque-1",
                media_type="text/plain",
                classification="unclassified",
                revision=1,
            ),
            SourceContent(object_id="report", version="opaque-1", content=b"hi"),
            mapping,
        )
