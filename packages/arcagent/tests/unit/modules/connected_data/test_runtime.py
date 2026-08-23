from __future__ import annotations

import asyncio

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPendingError, MappingPlan, SyncLimits
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SyncSource,
    SyncSourcePage,
)
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.ingest import ArcStoreObjectState
from arcagent.modules.connected_data.service import ConnectedDataService


class FakeSource:
    def __init__(self) -> None:
        self.closed = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="test",
            account_id="account",
            data_shape=SourceDataShape.DOCUMENT,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.started.set()
        await self.release.wait()
        return SyncSourcePage(next_checkpoint="", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"",
        )

    async def close_source(self) -> None:
        self.closed = True


class BrokenCloseSource(FakeSource):
    async def close_source(self) -> None:
        raise RuntimeError("close failed")


@pytest.mark.asyncio
async def test_catalog_revoke_waits_for_active_sync_lease() -> None:
    catalog = SourceCatalog()
    source = FakeSource()
    await catalog.register("dropbox", source)

    async with catalog.lease("dropbox") as registration:
        assert registration is not None
        revoke = asyncio.create_task(catalog.unregister("dropbox"))
        await asyncio.sleep(0)
        assert not revoke.done()
        assert not source.closed
    await revoke
    assert source.closed
    assert await catalog.snapshot() == ()


@pytest.mark.asyncio
async def test_catalog_close_contains_adapter_failure() -> None:
    catalog = SourceCatalog()
    first = BrokenCloseSource()
    second = FakeSource()
    await catalog.register("one", first)
    await catalog.register("two", second)
    await catalog.close()
    assert await catalog.snapshot() == ()
    assert second.closed


@pytest.mark.asyncio
async def test_service_degrades_without_arcstore_or_ingest() -> None:
    catalog = SourceCatalog()
    source = FakeSource()
    await catalog.register("dropbox", source)
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=None,
        ingest_factory=None,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=0.01,
    )
    await service.start()
    await asyncio.sleep(0.03)
    statuses = await service.list_sources()
    await service.close()
    assert statuses[0].status == "degraded"
    assert statuses[0].detail == "arcstore_unavailable"
    assert not source.closed


class _SnapshotSource:
    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="email",
            account_id="mailbox",
            data_shape=SourceDataShape.MAIL,
        )

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if request.checkpoint is not None:
            return SyncSourcePage(next_checkpoint=request.checkpoint, has_more=False)
        from arcagent.extension.source import SourceObject, SourceObjectKind

        return SyncSourcePage(
            objects=(
                SourceObject(
                    object_id="message-1",
                    locator="inbox/message-1",
                    kind=SourceObjectKind.FILE,
                    version="1",
                    media_type="text/plain",
                ),
            ),
            next_checkpoint="cursor-1",
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"Alpha launch notes",
        )

    async def close_source(self) -> None:
        return None


class _ApprovalGatedIngest:
    def __init__(self) -> None:
        self.approved = False
        self.ingested: list[str] = []
        self.stage_calls: list[tuple[KnowledgeHome, ...]] = []
        self.reset_calls = 0
        self.purge_calls = 0

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        if not self.approved:
            raise MappingPendingError()
        return MappingPlan(
            mapping_id="approval-1",
            homes=(KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT),
            revision="r1",
            content_hash="h1",
        )

    async def ingest(self, source, source_object, content, mapping) -> None:  # type: ignore[no-untyped-def]
        assert content is not None
        self.ingested.append(content.object_id)

    def allowed_homes(self, source: SourceDescription) -> tuple[KnowledgeHome, ...]:
        return (KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)

    async def stage_mapping(
        self, source: SourceDescription, homes: tuple[KnowledgeHome, ...]
    ) -> str:
        self.stage_calls.append(homes)
        return "approval-1"

    def canonical_source_id(self, source: SourceDescription) -> str:
        return "source-mailbox"

    async def reset_source(self, source: SourceDescription) -> None:
        self.reset_calls += 1

    async def purge_source(self, source: SourceDescription) -> None:
        self.purge_calls += 1


@pytest.mark.asyncio
async def test_mapping_gate_then_approval_backfill_and_restart_checkpoint() -> None:
    catalog = SourceCatalog()
    await catalog.register("mail", _SnapshotSource())
    state = InMemorySourceSyncStore()
    ingest = _ApprovalGatedIngest()

    async def open_state() -> InMemorySourceSyncStore:
        return state

    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=open_state,
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=60,
    )
    await service.start()
    await _wait_for_status(service, "awaiting_mapping")
    pending = (await service.list_sources())[0]
    assert pending.status == "awaiting_mapping"
    proposal = await service.stage_mapping(
        "mail", homes=(KnowledgeHome.MEMORY, KnowledgeHome.DOCUMENT)
    )
    assert proposal is not None
    assert proposal.source_id == "source-mailbox"
    assert proposal.approval_id == "approval-1"

    ingest.approved = True
    assert (await service.sync_now("mail")).status == "scheduled"
    await _wait_for_status(service, "complete")
    assert ingest.ingested == ["message-1"]
    assert (await state.get_state("did:agent", "mail")).cursor == "cursor-1"
    await service.close()

    restarted = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=open_state,
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
        interval_seconds=60,
    )
    await restarted.start()
    await _wait_for_status(restarted, "complete")
    assert ingest.ingested == ["message-1"]
    await restarted.close()


@pytest.mark.asyncio
async def test_revoke_purges_before_unregistering_source() -> None:
    catalog = SourceCatalog()
    source = _SnapshotSource()
    await catalog.register("mail", source)
    ingest = _ApprovalGatedIngest()
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=lambda: _ready(InMemorySourceSyncStore()),
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
    )
    await service.start()
    result = await service.revoke("mail")
    await service.close()
    assert result.status == "revoked"
    assert ingest.purge_calls == 1
    assert await catalog.snapshot() == ()


class _InterleavingObjectBackend(FakeBackend):
    """Force both object writes to overlap like concurrent page ingestion."""

    def __init__(self) -> None:
        super().__init__()
        self._object_writes = 0
        self._both_writes_started = asyncio.Event()

    async def mutable_write(self, collection, key, value, **kwargs):  # type: ignore[no-untyped-def]
        if collection == "connected_data_objects":
            self._object_writes += 1
            if self._object_writes == 2:
                self._both_writes_started.set()
            await self._both_writes_started.wait()
        await super().mutable_write(collection, key, value, **kwargs)


@pytest.mark.asyncio
async def test_object_state_source_index_survives_forced_interleaving() -> None:
    """No read-modify-write list may lose an object under concurrent ingestion."""
    from arcmemory.connected_data import ConnectedObjectState

    state = ArcStoreObjectState(_InterleavingObjectBackend(), actor_did="did:agent")
    await asyncio.gather(
        state.put_object_state("source", "first", ConnectedObjectState(version="1")),
        state.put_object_state("source", "second", ConnectedObjectState(version="1")),
    )

    assert await state.list_object_ids("source") == ["first", "second"]


@pytest.mark.asyncio
async def test_reindex_resets_artifacts_before_scheduling_snapshot() -> None:
    catalog = SourceCatalog()
    await catalog.register("mail", _SnapshotSource())
    ingest = _ApprovalGatedIngest()
    state = InMemorySourceSyncStore()
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=lambda: _ready(state),
        ingest_factory=lambda _: ingest,
        limits=SyncLimits(),
        global_concurrency=1,
    )
    await service.start()

    result = await service.reindex("mail")

    await service.close()
    assert result.status == "scheduled"
    assert ingest.reset_calls == 1


@pytest.mark.asyncio
async def test_revoke_fails_closed_when_source_purge_fails() -> None:
    class _FailingPurge(_ApprovalGatedIngest):
        async def purge_source(self, source: SourceDescription) -> None:
            raise RuntimeError("purge failed")

    catalog = SourceCatalog()
    await catalog.register("mail", _SnapshotSource())
    service = ConnectedDataService(
        catalog,
        agent_did="did:agent",
        sync_store_opener=lambda: _ready(InMemorySourceSyncStore()),
        ingest_factory=lambda _: _FailingPurge(),
        limits=SyncLimits(),
        global_concurrency=1,
    )
    await service.start()

    result = await service.revoke("mail")

    await service.close()
    assert result.status == "refused"
    assert (await catalog.snapshot())[0].connection_id == "mail"


async def _ready(value: InMemorySourceSyncStore) -> InMemorySourceSyncStore:
    return value


async def _wait_for_status(service: ConnectedDataService, expected: str) -> None:
    for _ in range(100):
        statuses = await service.list_sources()
        if statuses and statuses[0].status == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"source did not reach {expected!r}: {await service.list_sources()!r}")
